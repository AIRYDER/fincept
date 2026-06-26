from __future__ import annotations

import hashlib
import re
from urllib.parse import urlsplit, urlunsplit

from fincept_core.clock import now_ns
from fincept_core.schemas import InformationEvent

SOURCE_QUALITY_BY_TYPE: dict[str, float] = {
    "alpaca_news": 0.75,
    "newsapi": 0.65,
    "exa": 0.70,
    "openbb": 0.80,
}
SOURCE_QUALITY_BY_NAME: dict[str, float] = {
    "benzinga": 0.72,
    "reuters": 0.90,
    "bloomberg": 0.90,
    "associated press": 0.85,
    "wall street journal": 0.88,
    "financial times": 0.88,
    "cnbc": 0.76,
    "marketwatch": 0.72,
}
_EVENT_CATEGORY_PATTERNS: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...] = (
    (
        "earnings",
        (re.compile(r"\b(earnings|eps|revenue|guidance|quarterly results)\b", re.I),),
    ),
    (
        "regulatory",
        (
            re.compile(
                r"\b(sec|doj|ftc|fda|regulator|regulatory|antitrust|lawsuit|probe)\b",
                re.I,
            ),
        ),
    ),
    (
        "macro",
        (
            re.compile(
                r"\b(fed|fomc|inflation|cpi|jobs report|payrolls|rates|treasury|gdp)\b",
                re.I,
            ),
        ),
    ),
    (
        "product",
        (re.compile(r"\b(launch|unveil|product|chip|model|platform|supply)\b", re.I),),
    ),
    (
        "security",
        (
            re.compile(
                r"\b(hack|breach|exploit|ransomware|vulnerability|security)\b", re.I
            ),
        ),
    ),
    (
        "partnership",
        (
            re.compile(
                r"\b(partner|partnership|collaboration|deal|contract|agreement)\b", re.I
            ),
        ),
    ),
    (
        "analyst",
        (
            re.compile(
                r"\b(upgrade|downgrade|price target|initiates|maintains|analyst)\b",
                re.I,
            ),
        ),
    ),
    (
        "market_move",
        (
            re.compile(
                r"\b(shares|stock|surges|plunges|jumps|falls|rallies|sinks)\b", re.I
            ),
        ),
    ),
)
_NON_WORD = re.compile(r"[^a-z0-9]+")
_TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "fbclid",
    "gclid",
}


def enrich_information_event(
    event: InformationEvent, *, observed_at_ns: int | None = None
) -> InformationEvent:
    observed = observed_at_ns if observed_at_ns is not None else now_ns()
    symbols = normalize_symbols(event.symbols)
    entities = normalize_entities(
        [*event.entities, *symbols], known_symbols=set(symbols)
    )
    category = event.event_category or classify_event_category(
        event.headline, event.body
    )
    dedupe_key = normalize_dedupe_key(event)
    dedupe_group_id = event.dedupe_group_id or group_id_for(
        event, dedupe_key=dedupe_key
    )
    source_quality = event.source_quality
    if source_quality is None:
        source_quality = source_quality_for(event.source_type, event.source)
    recency_score = event.recency_score
    if recency_score is None:
        recency_score = score_recency(event.ts_event, observed_at_ns=observed)
    novelty_score = event.novelty_score if event.novelty_score is not None else 1.0
    metadata = dict(event.metadata)
    metadata.setdefault("enriched_by", "information_enricher.v1")
    return event.model_copy(
        update={
            "symbols": symbols,
            "entities": entities,
            "event_category": category,
            "source_quality": source_quality,
            "dedupe_key": dedupe_key,
            "dedupe_group_id": dedupe_group_id,
            "novelty_score": novelty_score,
            "recency_score": recency_score,
            "metadata": metadata,
        }
    )


def normalize_symbols(symbols: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for symbol in symbols:
        value = str(symbol).strip().upper()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def normalize_entities(
    entities: list[str], *, known_symbols: set[str] | None = None
) -> list[str]:
    known_symbols = known_symbols or set()
    out: list[str] = []
    seen: set[str] = set()
    for entity in entities:
        value = str(entity).strip()
        if not value:
            continue
        key = value.upper()
        if key in seen:
            continue
        seen.add(key)
        out.append(key if key in known_symbols or _looks_like_symbol(value) else value)
    return out


def normalize_dedupe_key(event: InformationEvent) -> str:
    if event.url:
        normalized = normalize_url(event.url)
        if normalized:
            return f"url:{normalized}"
    headline_slug = slugify(event.headline)[:120]
    symbols = ",".join(normalize_symbols(event.symbols))
    return f"headline:{symbols}:{headline_slug}"


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    if not parts.netloc:
        return ""
    query_pairs = []
    if parts.query:
        for raw_pair in parts.query.split("&"):
            key = raw_pair.split("=", 1)[0].lower()
            if key and key not in _TRACKING_PARAMS:
                query_pairs.append(raw_pair)
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(
        (
            parts.scheme.lower() or "https",
            parts.netloc.lower(),
            path,
            "&".join(query_pairs),
            "",
        )
    )


def slugify(text: str) -> str:
    return _NON_WORD.sub("-", text.strip().lower()).strip("-")


def group_id_for(event: InformationEvent, *, dedupe_key: str) -> str:
    symbols = ",".join(normalize_symbols(event.symbols))
    category = event.event_category or classify_event_category(
        event.headline, event.body
    )
    basis = f"{symbols}|{category}|{slugify(event.headline)[:80]}|{dedupe_key}"
    digest = hashlib.sha256(basis.encode()).hexdigest()[:16]
    return f"info:{digest}"


def source_quality_for(source_type: str, source: str) -> float:
    source_name = source.strip().lower()
    if source_name in SOURCE_QUALITY_BY_NAME:
        return SOURCE_QUALITY_BY_NAME[source_name]
    return SOURCE_QUALITY_BY_TYPE.get(source_type.strip().lower(), 0.60)


def classify_event_category(headline: str, body: str = "") -> str:
    text = f"{headline}\n{body}"
    for category, patterns in _EVENT_CATEGORY_PATTERNS:
        if any(pattern.search(text) for pattern in patterns):
            return category
    return "general"


def score_recency(ts_event: int, *, observed_at_ns: int) -> float:
    age_ns = max(0, observed_at_ns - ts_event)
    half_life_ns = 12 * 60 * 60 * 1_000_000_000
    half_lives = min(age_ns / half_life_ns, 50.0)
    return float(0.5**half_lives)


def _looks_like_symbol(value: str) -> bool:
    stripped = value.strip()
    return stripped == stripped.upper() and bool(
        re.fullmatch(r"[A-Z]{1,6}([.-][A-Z]{1,5})?", stripped)
    )
