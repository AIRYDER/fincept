from __future__ import annotations

import fakeredis.aioredis
import pytest

from agents.sentiment_features.main import GROUP_NAME, handle_sentiment_event
from agents.sentiment_features.store import FREQ, SentimentFeatureStore
from features.store import OnlineStore
from fincept_bus.producer import Producer
from fincept_bus.streams import STREAM_FEATURES_ONLINE
from fincept_core.events import Event, deserialize
from fincept_core.schemas import FeatureFrame, SentimentSignal

NS_PER_MIN = 60 * 1_000_000_000


def _signal(
    *,
    symbol: str = "NVDA",
    ts_event: int = 100 * NS_PER_MIN,
    score: float = 0.5,
    confidence: float = 0.8,
    event_type: str | None = "earnings",
    source_url: str | None = "https://example.com/a",
    agent_id: str = "sentiment_agent.v1",
) -> SentimentSignal:
    return SentimentSignal(
        agent_id=agent_id,
        symbol=symbol,
        ts_event=ts_event,
        score=score,
        confidence=confidence,
        event_type=event_type,
        source_url=source_url,
        source_excerpt="headline",
        entities=[symbol],
    )


async def test_store_writes_disjoint_sentiment_feature_frame() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    store = SentimentFeatureStore(redis, windows_min=(5, 30))

    try:
        frame = await store.add_signal(_signal(score=0.5, confidence=0.8))

        assert frame.freq == FREQ
        assert frame.symbol == "NVDA"
        assert frame.values["sentiment_5m"] == pytest.approx(0.5)
        assert frame.values["sentiment_5m_confidence"] == pytest.approx(0.8)
        assert frame.values["sentiment_5m_article_count"] == 1.0
        assert frame.tags["latest_event_category"] == "earnings"
        online = OnlineStore(redis)
        assert await online.get_latest("NVDA", "1m") is None
        cached = await online.get_latest("NVDA", FREQ)
        assert cached == frame
    finally:
        await redis.aclose()


async def test_store_computes_rolling_weighted_metrics() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    store = SentimentFeatureStore(redis, windows_min=(5, 30))
    base = 100 * NS_PER_MIN

    try:
        await store.add_signal(
            _signal(
                ts_event=base - 20 * NS_PER_MIN,
                score=-0.5,
                confidence=0.4,
                event_type="regulatory",
                source_url="https://source-a.example/x",
            )
        )
        await store.add_signal(
            _signal(
                ts_event=base - 2 * NS_PER_MIN,
                score=0.8,
                confidence=0.6,
                event_type="product",
                source_url="https://source-b.example/y",
            )
        )
        frame = await store.add_signal(
            _signal(
                ts_event=base,
                score=-0.2,
                confidence=1.0,
                event_type="earnings",
                source_url="https://source-b.example/z",
            )
        )

        assert frame.values["sentiment_5m"] == pytest.approx(0.175)
        assert frame.values["sentiment_5m_article_count"] == 2.0
        assert frame.values["sentiment_5m_unique_sources"] == 1.0
        assert frame.values["sentiment_5m_max_negative_urgency"] == pytest.approx(0.2)
        assert frame.values["sentiment_30m"] == pytest.approx(0.04)
        assert frame.values["sentiment_30m_article_count"] == 3.0
        assert frame.values["sentiment_30m_unique_sources"] == 2.0
        assert frame.values["sentiment_30m_disagreement"] is not None
        assert frame.values["sentiment_30m_disagreement"] > 0.0
        assert frame.tags["latest_event_category"] == "earnings"
    finally:
        await redis.aclose()


async def test_store_trims_old_observations_and_dedupes_replays() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    store = SentimentFeatureStore(redis, windows_min=(5, 30))
    base = 100 * NS_PER_MIN
    duplicate = _signal(ts_event=base, score=0.3, confidence=0.7)

    try:
        await store.add_signal(_signal(ts_event=base - 31 * NS_PER_MIN, score=-0.9))
        await store.add_signal(duplicate)
        frame = await store.add_signal(duplicate)

        observations = await store.read_observations("NVDA")
        assert len(observations) == 1
        assert frame.values["sentiment_30m_article_count"] == 1.0
        assert frame.values["sentiment_30m"] == pytest.approx(0.3)
    finally:
        await redis.aclose()


async def test_store_refresh_recomputes_decayed_windows() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    store = SentimentFeatureStore(redis, windows_min=(5, 30))
    base = 100 * NS_PER_MIN

    try:
        await store.add_signal(_signal(ts_event=base, score=0.5, confidence=0.8))
        frame = await store.refresh_symbol("NVDA", ts_event=base + 6 * NS_PER_MIN)

        assert frame is not None
        assert frame.ts_event == base + 6 * NS_PER_MIN
        assert frame.values["sentiment_5m"] is None
        assert frame.values["sentiment_5m_article_count"] == 0.0
        assert frame.values["sentiment_30m"] == pytest.approx(0.5)
        assert frame.values["sentiment_30m_article_count"] == 1.0
    finally:
        await redis.aclose()


async def test_handle_sentiment_event_publishes_feature_frame() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    store = SentimentFeatureStore(redis, windows_min=(5, 30))
    producer = Producer(redis)

    try:
        await handle_sentiment_event(
            Event(type="sentiment", payload=_signal()),
            store=store,
            producer=producer,
        )

        entries = await redis.xrange(STREAM_FEATURES_ONLINE)
        assert len(entries) == 1
        event = deserialize(entries[0][1])
        assert event.type == "feature_frame"
        assert isinstance(event.payload, FeatureFrame)
        assert event.payload.freq == FREQ
        assert event.payload.values["sentiment_5m_article_count"] == 1.0
    finally:
        await redis.aclose()


async def test_handle_sentiment_event_ignores_wrong_payload() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    store = SentimentFeatureStore(redis, windows_min=(5, 30))
    producer = Producer(redis)

    try:
        await handle_sentiment_event(
            Event(
                type="feature_frame",
                payload=FeatureFrame(symbol="NVDA", ts_event=1, freq="1m", values={}),
            ),
            store=store,
            producer=producer,
        )

        assert await redis.xlen(STREAM_FEATURES_ONLINE) == 0
    finally:
        await redis.aclose()


def test_consumer_group_is_independent_from_orchestrator() -> None:
    assert GROUP_NAME == "sentiment_features.v1"
    assert GROUP_NAME != "orchestrator"
