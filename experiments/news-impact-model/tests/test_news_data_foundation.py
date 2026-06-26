from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from news_impact_model.events import (  # noqa: E402
    EntityLinker,
    classify_event_type,
    dedupe_group_id,
    dedupe_news_events,
    load_vendor_news_events,
    normalize_vendor_event,
    source_latency_stats,
)
from news_impact_model.labels import label_event_impact  # noqa: E402
from news_impact_model.schema import PricePoint  # noqa: E402


def test_normalize_vendor_event_preserves_exact_availability_timestamp() -> None:
    event = normalize_vendor_event(
        {
            "id": "abc-123",
            "source": "Reuters",
            "title": "Acme raises full-year guidance",
            "summary": "Revenue guidance rose after strong demand.",
            "url": "https://example.com/acme?utm_source=feed",
            "published_at": "2026-05-17T14:00:00Z",
            "available_at_ns": 1_779_026_403_456_789_000,
            "symbols": ["ACME"],
        },
        source_type="reuters_export",
    )

    assert event.event_id == "reuters_export:abc-123"
    assert event.available_at_ns == 1_779_026_403_456_789_000
    assert event.published_at_ns == 1_779_026_400_000_000_000
    assert event.url == "https://example.com/acme"
    assert event.event_type == "guidance"
    assert event.symbols == ("ACME",)


def test_load_vendor_news_events_jsonl_uses_entity_linker(tmp_path: Path) -> None:
    path = tmp_path / "vendor.jsonl"
    path.write_text(
        json.dumps(
            {
                "provider_event_id": "row-1",
                "source": "Benzinga",
                "headline": "Apple unveils new iPhone supply plan",
                "body": "Suppliers expect a stronger launch cycle.",
                "published_at": "2026-05-17T14:00:00Z",
                "received_at": "2026-05-17T14:00:07Z",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    linker = EntityLinker(
        {
            "AAPL": ("Apple", "Apple Inc.", "iPhone"),
            "MSFT": ("Microsoft",),
        }
    )

    events = load_vendor_news_events(path, source_type="benzinga", entity_linker=linker)

    assert len(events) == 1
    assert events[0].event_id == "benzinga:row-1"
    assert events[0].symbols == ("AAPL",)
    assert events[0].available_at_ns - events[0].published_at_ns == 7_000_000_000


def test_entity_linker_avoids_generic_uppercase_false_positives() -> None:
    linker = EntityLinker({"AAPL": ("Apple", "Apple Inc.")})

    assert linker.link("AI demand rises across software sector") == ()
    assert linker.link("Apple shares rise as services revenue improves") == ("AAPL",)


def test_event_classifier_covers_financial_event_taxonomy() -> None:
    assert classify_event_type("Acme issues weak Q2 guidance") == "guidance"
    assert classify_event_type("Acme sued over antitrust claims") == "litigation"
    assert classify_event_type("Acme to acquire Beta in $2B deal") == "m&a"
    assert classify_event_type("Acme prices convertible note offering") == "financing"
    assert classify_event_type("Fed decision lifts Treasury yields") == "macro"


def test_dedupe_groups_same_story_across_vendors() -> None:
    first = normalize_vendor_event(
        {
            "id": "r1",
            "source": "Reuters",
            "headline": "Acme raises full-year guidance after demand jump",
            "published_at": "2026-05-17T14:00:00Z",
            "available_at": "2026-05-17T14:00:05Z",
            "symbols": ["ACME"],
            "url": "https://reuters.example/acme-guidance?utm_campaign=x",
        },
        source_type="reuters",
    )
    second = normalize_vendor_event(
        {
            "id": "b1",
            "source": "Benzinga",
            "title": "Acme raises full year guidance after demand jump",
            "published_at": "2026-05-17T14:01:00Z",
            "available_at": "2026-05-17T14:01:02Z",
            "symbols": ["ACME"],
            "url": "https://benzinga.example/acme-guidance",
        },
        source_type="benzinga",
    )

    assert dedupe_group_id(first) == dedupe_group_id(second)
    assert dedupe_news_events([second, first]) == [first]


def test_source_latency_stats_summarize_vendor_reliability() -> None:
    events = [
        normalize_vendor_event(
            {
                "id": "r1",
                "source": "Reuters",
                "headline": "Acme launches product",
                "published_at": "2026-05-17T14:00:00Z",
                "available_at": "2026-05-17T14:00:02Z",
                "symbols": ["ACME"],
            },
            source_type="reuters",
        ),
        normalize_vendor_event(
            {
                "id": "r2",
                "source": "Reuters",
                "headline": "Acme expands product",
                "published_at": "2026-05-17T14:10:00Z",
                "available_at": "2026-05-17T14:10:06Z",
                "symbols": ["ACME"],
            },
            source_type="reuters",
        ),
    ]

    stats = source_latency_stats(events)

    assert len(stats) == 1
    assert stats[0].source == "reuters"
    assert stats[0].event_count == 2
    assert stats[0].mean_latency_s == 4.0
    assert stats[0].p95_latency_s == 5.8
    assert stats[0].symbol_coverage == {"ACME": 2}


def test_label_event_impact_supports_beta_adjusted_abnormal_returns() -> None:
    labels = label_event_impact(
        event_available_at_ns=100,
        asset_prices=[
            PricePoint(ts_ns=90, price=100.0),
            PricePoint(ts_ns=160, price=106.0),
        ],
        benchmark_prices=[
            PricePoint(ts_ns=90, price=200.0),
            PricePoint(ts_ns=160, price=204.0),
        ],
        horizons_ns={"1m": 60},
        asset_beta=1.5,
    )

    assert round(labels.abnormal_returns["1m"], 4) == 0.03
