from __future__ import annotations

from fincept_core.schemas import FeatureFrame

DEFAULT_FEATURES: list[str] = [
    "sentiment_5m",
    "sentiment_5m_confidence",
    "sentiment_5m_article_count",
    "sentiment_5m_unique_sources",
    "sentiment_5m_disagreement",
    "sentiment_5m_max_negative_urgency",
    "sentiment_30m",
    "sentiment_30m_confidence",
    "sentiment_30m_article_count",
    "sentiment_30m_unique_sources",
    "sentiment_30m_disagreement",
    "sentiment_30m_max_negative_urgency",
    "sentiment_240m",
    "sentiment_240m_confidence",
    "sentiment_240m_article_count",
    "sentiment_240m_unique_sources",
    "sentiment_240m_disagreement",
    "sentiment_240m_max_negative_urgency",
]

DEFAULTABLE_FEATURES: set[str] = {
    "sentiment_5m",
    "sentiment_5m_confidence",
    "sentiment_5m_article_count",
    "sentiment_5m_unique_sources",
    "sentiment_5m_disagreement",
    "sentiment_5m_max_negative_urgency",
}


def extract_sentiment_row(
    frame: FeatureFrame,
    *,
    feature_names: list[str] | None = None,
    allow_defaults: bool = True,
) -> dict[str, float] | None:
    if frame.freq != "sentiment":
        return None
    names = feature_names or DEFAULT_FEATURES
    row: dict[str, float] = {}
    for name in names:
        value = frame.values.get(name)
        if value is None and allow_defaults and name in DEFAULTABLE_FEATURES:
            value = 0.0
        if value is None:
            return None
        row[name] = float(value)
    return row
