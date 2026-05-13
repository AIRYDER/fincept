from __future__ import annotations

from decimal import Decimal

import fakeredis.aioredis
import pytest

from agents.news_outcome_labeler.main import handle_event, read_mark, read_mark_at_or_after
from agents.news_outcome_labeler.store import NewsOutcomeStore
from fincept_core.events import Event
from fincept_core.schemas import AssetClass, FeatureFrame, TradeEvent, Venue

NS_PER_MIN = 60 * 1_000_000_000


def _frame(**overrides: object) -> FeatureFrame:
    payload = {
        "symbol": "NVDA",
        "ts_event": 100 * NS_PER_MIN,
        "freq": "sentiment",
        "values": {"sentiment_30m": 0.5, "sentiment_30m_article_count": 1.0},
        "tags": {"latest_event_category": "earnings"},
    }
    payload.update(overrides)
    return FeatureFrame.model_validate(payload)


def _trade(*, ts_event: int = 100 * NS_PER_MIN, price: Decimal = Decimal("100")) -> TradeEvent:
    return TradeEvent(
        venue=Venue.ALPACA,
        symbol="NVDA",
        asset_class=AssetClass.EQUITY,
        ts_event=ts_event,
        ts_recv=ts_event,
        price=price,
        size=Decimal("1"),
    )


async def test_capture_snapshot_and_label_due() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    store = NewsOutcomeStore(redis, horizons_ns={"5m": 5 * NS_PER_MIN})
    frame = _frame()

    try:
        example_id = await store.capture_snapshot(frame, start_price=Decimal("100"))
        assert example_id is not None

        async def lookup(symbol: str, ts_event: int) -> Decimal | None:
            assert symbol == "NVDA"
            assert ts_event == frame.ts_event + 5 * NS_PER_MIN
            return Decimal("105")

        labels = await store.label_due(
            now_ns=frame.ts_event + 5 * NS_PER_MIN,
            price_lookup=lookup,
        )

        assert len(labels) == 1
        assert labels[0].example_id == example_id
        assert labels[0].return_value == pytest.approx(0.05)
    finally:
        await redis.aclose()


async def test_handle_event_updates_marks_and_captures_sentiment_frame() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    store = NewsOutcomeStore(redis, horizons_ns={"5m": 5 * NS_PER_MIN})

    try:
        await handle_event(
            Event(type="trade", payload=_trade(price=Decimal("101"))),
            redis=redis,
            store=store,
        )
        assert await read_mark(redis, "NVDA") == Decimal("101")
        assert await read_mark_at_or_after(redis, "NVDA", 0) == Decimal("101")

        await handle_event(
            Event(type="feature_frame", payload=_frame()),
            redis=redis,
            store=store,
        )

        pending = await redis.zrange("news_alpha:pending_labels", 0, -1)
        assert len(pending) == 1
    finally:
        await redis.aclose()
