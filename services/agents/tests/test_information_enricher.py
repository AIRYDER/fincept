from __future__ import annotations

import fakeredis.aioredis

from agents.information_enricher.enrich import enrich_information_event
from agents.information_enricher.main import handle_information_event
from fincept_bus.producer import Producer
from fincept_bus.streams import STREAM_INFO_ENRICHED
from fincept_core.events import Event, deserialize
from fincept_core.schemas import InformationEvent


def _event(**overrides: object) -> InformationEvent:
    payload = {
        "event_id": "alpaca_news:n1",
        "source": "Benzinga",
        "source_type": "alpaca_news",
        "headline": "Nvidia expands AI chip supply after earnings beat",
        "body": "Revenue guidance improved after strong datacenter demand.",
        "url": "HTTPS://Example.com/NVDA/?utm_source=x&keep=1",
        "published_at": "2026-05-05T12:00:00Z",
        "ts_event": 1_000_000_000,
        "symbols": ["nvda", "NVDA", " msft "],
        "entities": ["Nvidia", "nvda"],
        "information_type": "news",
        "raw_payload_ref": "news:article:n1",
        "dedupe_key": "url:https://example.com/nvda/",
    }
    payload.update(overrides)
    return InformationEvent.model_validate(payload)


def test_enrich_information_event_normalizes_operational_fields() -> None:
    enriched = enrich_information_event(
        _event(source_quality=None), observed_at_ns=1_000_000_000
    )

    assert enriched.symbols == ["NVDA", "MSFT"]
    assert enriched.entities == ["Nvidia", "NVDA", "MSFT"]
    assert enriched.event_category == "earnings"
    assert enriched.source_quality == 0.72
    assert enriched.dedupe_key == "url:https://example.com/NVDA?keep=1"
    assert enriched.dedupe_group_id is not None
    assert enriched.dedupe_group_id.startswith("info:")
    assert enriched.novelty_score == 1.0
    assert enriched.recency_score == 1.0
    assert enriched.metadata["enriched_by"] == "information_enricher.v1"


def test_enrich_information_event_classifies_regulatory_without_url() -> None:
    enriched = enrich_information_event(
        _event(
            headline="SEC opens probe into exchange disclosures",
            body="The regulator requested documents.",
            url=None,
            symbols=["coin"],
        ),
        observed_at_ns=13 * 60 * 60 * 1_000_000_000,
    )

    assert enriched.event_category == "regulatory"
    assert enriched.dedupe_key.startswith("headline:COIN:sec-opens-probe")
    assert 0.0 < enriched.recency_score < 1.0


async def test_handle_information_event_publishes_enriched_event() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    producer = Producer(redis)

    try:
        await handle_information_event(
            Event(type="information", payload=_event(source_quality=None)),
            producer=producer,
        )

        entries = await redis.xrange(STREAM_INFO_ENRICHED)
        assert len(entries) == 1
        event = deserialize(entries[0][1])
        assert event.type == "information"
        assert isinstance(event.payload, InformationEvent)
        assert event.payload.event_category == "earnings"
        assert event.payload.symbols == ["NVDA", "MSFT"]
        assert event.payload.source_quality == 0.72
        assert event.payload.metadata["enriched_by"] == "information_enricher.v1"
    finally:
        await redis.aclose()
