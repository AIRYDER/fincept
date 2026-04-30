from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class NewsEvent:
    """Point-in-time representation of one news item.

    ``available_at_ns`` must be the time the system could have acted on the
    item, not the article's authoring or later crawl time.
    """

    event_id: str
    available_at_ns: int
    source: str
    headline: str
    body: str = ""
    symbols: tuple[str, ...] = ()
    event_type: str = "general"
    language: str = "en"
    source_priority: float | None = None

    @property
    def text(self) -> str:
        return f"{self.headline}\n{self.body}".strip()


@dataclass(frozen=True)
class MarketContext:
    """Pre-news state for one affected symbol."""

    symbol: str
    market_regime: str = "unknown"
    pre_event_return: float | None = None
    realized_volatility: float | None = None
    relative_volume: float | None = None
    spread_bps: float | None = None
    liquidity_score: float | None = None


@dataclass(frozen=True)
class HistoricalOutcome:
    """Historical news item plus realized market reaction labels."""

    event_id: str
    available_at_ns: int
    source: str
    headline: str
    body: str
    symbols: tuple[str, ...]
    event_type: str
    market_regime: str
    abnormal_returns: dict[str, float]
    volatility_impact: float = 0.0
    volume_impact: float = 0.0
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return f"{self.headline}\n{self.body}".strip()


@dataclass(frozen=True)
class PricePoint:
    """One price observation used for historical impact labeling."""

    ts_ns: int
    price: float


@dataclass(frozen=True)
class ImpactLabels:
    """Realized labels for a single event and affected asset."""

    abnormal_returns: dict[str, float]
    max_favorable_return: float
    max_adverse_return: float


@dataclass(frozen=True)
class AnalogMatch:
    """Scored historical analog returned by the retrieval layer."""

    outcome: HistoricalOutcome
    score: float
    text_overlap: float
    symbol_match: bool
    event_type_match: bool
    regime_match: bool


@dataclass(frozen=True)
class HorizonImpact:
    """Predicted impact distribution for one forecast horizon."""

    expected_return: float
    p_up: float
    q10: float
    q50: float
    q90: float
    sample_size: int


@dataclass(frozen=True)
class SimilarEventSummary:
    """Compact historical analog summary safe for API/dashboard payloads."""

    event_id: str
    source: str
    headline: str
    event_type: str
    score: float
    abnormal_returns: dict[str, float]


@dataclass(frozen=True)
class NewsImpactPrediction:
    """Raw market-impact prediction produced before trade decisions."""

    event_id: str
    symbol: str
    event_type: str
    horizons: dict[str, HorizonImpact]
    volatility_impact: float
    volume_impact: float
    confidence: float
    similar_events: list[SimilarEventSummary]
    model_version: str = "news-impact-analog-baseline-v0"
