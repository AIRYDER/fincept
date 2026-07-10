from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fakeredis.aioredis import FakeRedis
from fincept_bus.producer import Producer
from fincept_bus.streams import STREAM_SIG_NEWS_IMPACT
from fincept_core.events import Event, deserialize
from fincept_core.schemas import InformationEvent, NewsImpactSignal
from news_impact_model.analogs import HistoricalAnalogIndex
from news_impact_model.model import NewsImpactModel
from news_impact_model.schema import HistoricalOutcome

from agents.news_impact_agent.main import (
    build_news_impact_signals,
    handle_information_event,
    information_to_news_event,
)


@pytest.fixture
async def redis_client() -> AsyncIterator[FakeRedis]:
    redis = FakeRedis()
    await redis.delete(STREAM_SIG_NEWS_IMPACT)
    yield redis
    await redis.aclose()


def _info(**overrides: object) -> InformationEvent:
    payload = {
        "event_id": "info-1",
        "source": "Reuters",
        "source_type": "reuters",
        "headline": "Acme raises full-year guidance after demand jump",
        "body": "Management lifted revenue outlook after order growth.",
        "url": "https://example.com/acme-guidance",
        "published_at": "2026-05-17T14:00:00Z",
        "ts_event": 1_779_026_400_000_000_000,
        "symbols": ["ACME"],
        "entities": ["ACME"],
        "information_type": "news",
        "event_category": "guidance",
        "source_quality": 0.9,
        "dedupe_key": "url:https://example.com/acme-guidance",
        "dedupe_group_id": "info:acme-guidance",
        "novelty_score": 1.0,
        "recency_score": 0.95,
        "metadata": {
            "available_at_ns": "1779026403456789000",
            "raw_provider_id": "r-1",
        },
    }
    payload.update(overrides)
    return InformationEvent.model_validate(payload)


def _model_with_history() -> NewsImpactModel:
    index = HistoricalAnalogIndex()
    index.add(
        HistoricalOutcome(
            event_id="hist-1",
            available_at_ns=1_770_000_000_000_000_000,
            source="reuters",
            headline="Acme raises guidance after demand jump",
            body="Revenue outlook improved after stronger orders.",
            symbols=("ACME",),
            event_type="guidance",
            market_regime="unknown",
            abnormal_returns={"5m": 0.018, "30m": 0.031},
            volatility_impact=0.2,
            volume_impact=0.7,
        )
    )
    return NewsImpactModel(index=index, horizons=("5m", "30m"), top_k=5)


def test_information_to_news_event_preserves_exact_available_at_ns() -> None:
    event = information_to_news_event(_info())

    assert event.event_id == "info-1"
    assert event.available_at_ns == 1_779_026_403_456_789_000
    assert event.symbols == ("ACME",)
    assert event.event_type == "guidance"


def test_no_signal_emits_when_no_analogs_exist() -> None:
    empty_model = NewsImpactModel(index=HistoricalAnalogIndex(), horizons=("5m",))

    signals = build_news_impact_signals(_info(), model=empty_model)

    assert signals == []


def test_signal_payload_has_no_order_or_sizing_fields() -> None:
    signals = build_news_impact_signals(_info(), model=_model_with_history())

    assert len(signals) == 1
    signal = signals[0]
    dumped = signal.model_dump()
    assert isinstance(signal, NewsImpactSignal)
    assert dumped["available_at_ns"] == 1_779_026_403_456_789_000
    assert dumped["horizons"]["5m"]["sample_size"] == 1
    assert dumped["source_urls"] == ["https://example.com/acme-guidance"]
    assert "side" not in dumped
    assert "quantity" not in dumped
    assert "target_notional_usd" not in dumped
    assert "venue" not in dumped


@pytest.mark.asyncio
async def test_handle_information_event_publishes_news_impact_stream(
    redis_client: FakeRedis,
) -> None:
    producer = Producer(redis_client)

    emitted = await handle_information_event(
        Event(type="information", payload=_info()),
        model=_model_with_history(),
        producer=producer,
    )

    assert emitted == 1
    messages = await redis_client.xrange(STREAM_SIG_NEWS_IMPACT)
    assert len(messages) == 1
    _, fields = messages[0]
    event = deserialize(fields)
    assert event.type == "news_impact"
    assert isinstance(event.payload, NewsImpactSignal)
    assert event.payload.event_id == "info-1"
    assert event.payload.available_at_ns == 1_779_026_403_456_789_000
