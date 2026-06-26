from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from news_impact_model.schema import NewsEvent

NS_PER_SECOND = 1_000_000_000
NS_PER_MILLISECOND = 1_000_000
NS_PER_MINUTE = 60 * NS_PER_SECOND

EVENT_TYPES = (
    "regulatory",
    "earnings",
    "guidance",
    "macro",
    "product",
    "security",
    "litigation",
    "partnership",
    "financing",
    "m&a",
    "general",
)

_TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "fbclid",
    "gclid",
}
_NON_WORD = re.compile(r"[^a-z0-9]+")
_CASHTAG = re.compile(r"(?<![A-Z0-9])\$([A-Z][A-Z0-9.-]{0,9})(?![A-Z0-9])")

_EVENT_PATTERNS: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...] = (
    ("guidance", (re.compile(r"\b(guidance|outlook|forecast|raises|cuts|lowers|full[- ]year)\b", re.I),)),
    ("earnings", (re.compile(r"\b(earnings|eps|revenue|quarterly results|profit|loss)\b", re.I),)),
    ("litigation", (re.compile(r"\b(lawsuit|sued|sues|settlement|class action|antitrust|probe|investigation)\b", re.I),)),
    ("m&a", (re.compile(r"\b(acquire|acquires|acquisition|merger|buyout|takeover|deal to buy)\b", re.I),)),
    ("financing", (re.compile(r"\b(offering|convertible note|debt sale|share sale|secondary|raises capital|financing)\b", re.I),)),
    ("macro", (re.compile(r"\b(fed|fomc|inflation|cpi|ppi|payrolls|jobs report|treasury|gdp|rates|yield)\b", re.I),)),
    ("security", (re.compile(r"\b(hack|breach|exploit|ransomware|vulnerability|cyberattack|security incident)\b", re.I),)),
    ("regulatory", (re.compile(r"\b(sec|doj|ftc|fda|regulator|regulatory|approval|approved|ban|sanction)\b", re.I),)),
    ("product", (re.compile(r"\b(launch|unveil|product|chip|model|platform|supply|device)\b", re.I),)),
    ("partnership", (re.compile(r"\b(partner|partnership|collaboration|contract|agreement|supplier)\b", re.I),)),
)


@dataclass(frozen=True)
class NormalizedNewsEvent:
    """Provider-neutral event used before impact labels are available."""

    event_id: str
    source: str
    source_type: str
    headline: str
    body: str
    url: str | None
    published_at_ns: int
    available_at_ns: int
    symbols: tuple[str, ...] = ()
    entities: tuple[str, ...] = ()
    event_type: str = "general"
    provider_event_id: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return f"{self.headline}\n{self.body}".strip()

    def to_news_event(self) -> NewsEvent:
        return NewsEvent(
            event_id=self.event_id,
            available_at_ns=self.available_at_ns,
            source=self.source,
            headline=self.headline,
            body=self.body,
            symbols=self.symbols,
            event_type=self.event_type,
        )


@dataclass(frozen=True)
class SourceLatencyStats:
    """Latency and coverage summary for a normalized vendor feed."""

    source: str
    event_count: int
    mean_latency_s: float
    p95_latency_s: float
    min_latency_s: float
    max_latency_s: float
    symbol_coverage: dict[str, int]


class EntityLinker:
    """Simple alias-based ticker linker for normalized vendor exports."""

    def __init__(self, aliases_by_symbol: Mapping[str, Iterable[str]]) -> None:
        self._aliases: dict[str, tuple[str, ...]] = {
            _normalize_symbol(symbol): tuple(
                alias.strip() for alias in aliases if alias and alias.strip()
            )
            for symbol, aliases in aliases_by_symbol.items()
        }
        self._ticker_set = set(self._aliases)

    def link(self, headline: str, body: str = "") -> tuple[str, ...]:
        text = f"{headline}\n{body}"
        found: list[str] = []
        for match in _CASHTAG.finditer(text):
            symbol = _normalize_symbol(match.group(1))
            if symbol in self._ticker_set and symbol not in found:
                found.append(symbol)

        for symbol, aliases in self._aliases.items():
            if symbol not in found and _contains_symbol_token(text, symbol):
                found.append(symbol)
            for alias in aliases:
                if symbol in found:
                    break
                if re.search(rf"(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])", text, re.I):
                    found.append(symbol)
        return tuple(found)


def load_vendor_news_events(
    path: str | Path,
    *,
    source_type: str,
    entity_linker: EntityLinker | None = None,
) -> list[NormalizedNewsEvent]:
    """Load provider exports from JSONL, JSON, or CSV into normalized events."""

    dataset_path = Path(path)
    suffix = dataset_path.suffix.lower()
    if suffix in {".jsonl", ".ndjson"}:
        rows = _iter_jsonl(dataset_path)
    elif suffix == ".json":
        rows = _iter_json(dataset_path)
    elif suffix == ".csv":
        rows = _iter_csv(dataset_path)
    else:
        raise ValueError(f"unsupported vendor news file type: {dataset_path.suffix}")
    return [
        normalize_vendor_event(row, source_type=source_type, entity_linker=entity_linker)
        for row in rows
    ]


def normalize_vendor_event(
    row: Mapping[str, Any],
    *,
    source_type: str,
    entity_linker: EntityLinker | None = None,
) -> NormalizedNewsEvent:
    """Normalize a vendor article row while preserving point-in-time availability."""

    source_type_norm = source_type.strip().lower()
    provider_event_id = _first_str(
        row,
        "provider_event_id",
        "raw_id",
        "uuid",
        "id",
        "event_id",
    )
    headline = _first_str(row, "headline", "title", required=True)
    body = _first_str(row, "body", "summary", "description")
    source = _first_str(row, "source", "provider", default=source_type_norm).lower()
    published_at_ns = _first_timestamp_ns(row, "published_at_ns", "published_at", "created_at_ns", "created_at")
    available_at_ns = _first_timestamp_ns(
        row,
        "available_at_ns",
        "available_at",
        "received_at_ns",
        "received_at",
        "ingested_at_ns",
        "ingested_at",
        default=published_at_ns,
    )
    symbols = _parse_symbols(row.get("symbols") or row.get("tickers"))
    linked_symbols: tuple[str, ...] = ()
    if entity_linker is not None:
        linked_symbols = entity_linker.link(headline, body)
    symbols = _dedupe_tuple((*symbols, *linked_symbols))
    event_type = _first_str(row, "event_type", "category")
    if not event_type:
        event_type = classify_event_type(headline, body)
    event_type = normalize_event_type(event_type)
    url = normalize_url(_first_str(row, "url", "link"))
    event_id = (
        f"{source_type_norm}:{provider_event_id}"
        if provider_event_id
        else f"{source_type_norm}:{_stable_event_digest(source, headline, available_at_ns)}"
    )
    metadata = _metadata_from_row(row)
    metadata.setdefault("dedupe_group_id", dedupe_group_id_fields(symbols, event_type, headline, available_at_ns))

    return NormalizedNewsEvent(
        event_id=event_id,
        source=source,
        source_type=source_type_norm,
        headline=headline,
        body=body,
        url=url,
        published_at_ns=published_at_ns,
        available_at_ns=available_at_ns,
        symbols=symbols,
        entities=tuple(symbols),
        event_type=event_type,
        provider_event_id=provider_event_id or None,
        metadata=metadata,
    )


def classify_event_type(headline: str, body: str = "") -> str:
    text = f"{headline}\n{body}"
    for event_type, patterns in _EVENT_PATTERNS:
        if any(pattern.search(text) for pattern in patterns):
            return event_type
    return "general"


def normalize_event_type(value: str) -> str:
    normalized = value.strip().lower().replace("_", " ")
    if normalized in {"ma", "m and a", "merger", "acquisition"}:
        return "m&a"
    normalized = normalized.replace(" ", "_")
    return normalized if normalized in EVENT_TYPES else "general"


def dedupe_group_id(event: NormalizedNewsEvent, *, bucket_minutes: int = 120) -> str:
    return dedupe_group_id_fields(
        event.symbols,
        event.event_type,
        event.headline,
        event.available_at_ns,
        bucket_minutes=bucket_minutes,
    )


def dedupe_group_id_fields(
    symbols: tuple[str, ...],
    event_type: str,
    headline: str,
    available_at_ns: int,
    *,
    bucket_minutes: int = 120,
) -> str:
    if bucket_minutes <= 0:
        raise ValueError("bucket_minutes must be positive")
    bucket = available_at_ns // (bucket_minutes * NS_PER_MINUTE)
    basis = "|".join(
        [
            ",".join(sorted(symbols)),
            normalize_event_type(event_type),
            _slugify(headline)[:120],
            str(bucket),
        ]
    )
    return f"news:{hashlib.sha256(basis.encode()).hexdigest()[:20]}"


def dedupe_news_events(events: Iterable[NormalizedNewsEvent]) -> list[NormalizedNewsEvent]:
    """Collapse same-story vendor duplicates, preferring earliest availability."""

    grouped: dict[str, NormalizedNewsEvent] = {}
    for event in events:
        group_id = dedupe_group_id(event)
        current = grouped.get(group_id)
        if current is None or _event_preference_key(event) < _event_preference_key(current):
            grouped[group_id] = event
    return sorted(grouped.values(), key=_event_preference_key)


def source_latency_stats(events: Iterable[NormalizedNewsEvent]) -> list[SourceLatencyStats]:
    grouped: dict[str, list[NormalizedNewsEvent]] = {}
    for event in events:
        grouped.setdefault(event.source, []).append(event)
    stats: list[SourceLatencyStats] = []
    for source, rows in sorted(grouped.items()):
        latencies = [
            max(0.0, (row.available_at_ns - row.published_at_ns) / NS_PER_SECOND)
            for row in rows
        ]
        coverage: dict[str, int] = {}
        for row in rows:
            for symbol in row.symbols:
                coverage[symbol] = coverage.get(symbol, 0) + 1
        stats.append(
            SourceLatencyStats(
                source=source,
                event_count=len(rows),
                mean_latency_s=round(mean(latencies), 6),
                p95_latency_s=round(_quantile(latencies, 0.95), 6),
                min_latency_s=round(min(latencies), 6),
                max_latency_s=round(max(latencies), 6),
                symbol_coverage=dict(sorted(coverage.items())),
            )
        )
    return stats


def normalize_url(url: str) -> str | None:
    if not url:
        return None
    parts = urlsplit(url.strip())
    if not parts.netloc:
        return None
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in _TRACKING_PARAMS
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(
        (
            parts.scheme.lower() or "https",
            parts.netloc.lower(),
            path,
            urlencode(query),
            "",
        )
    )


def parse_timestamp_ns(value: Any) -> int:
    if isinstance(value, int):
        return _coerce_epoch_number_to_ns(value)
    if isinstance(value, float):
        return _coerce_epoch_number_to_ns(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            raise ValueError("timestamp value is empty")
        if re.fullmatch(r"\d+(\.\d+)?", stripped):
            return _coerce_epoch_number_to_ns(float(stripped) if "." in stripped else int(stripped))
        normalized = stripped.replace("Z", "+00:00")
        parsed = dt.datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)  # noqa: UP017 - Python 3.10 compatible.
        else:
            parsed = parsed.astimezone(dt.timezone.utc)  # noqa: UP017 - Python 3.10 compatible.
        return int(parsed.timestamp() * NS_PER_SECOND)
    raise ValueError(f"unsupported timestamp value: {value!r}")


def _iter_jsonl(path: Path) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            row = json.loads(stripped)
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{line_no}: expected JSON object")
            rows.append(row)
    return rows


def _iter_json(path: Path) -> list[Mapping[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and "articles" in payload:
        payload = payload["articles"]
    if isinstance(payload, dict) and "events" in payload:
        payload = payload["events"]
    if not isinstance(payload, list):
        raise ValueError(f"{path}: expected a JSON array or object with articles/events")
    if not all(isinstance(row, dict) for row in payload):
        raise ValueError(f"{path}: every event row must be a JSON object")
    return payload


def _iter_csv(path: Path) -> list[Mapping[str, Any]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _first_str(
    row: Mapping[str, Any],
    *keys: str,
    default: str = "",
    required: bool = False,
) -> str:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return str(value).strip()
    if required:
        raise ValueError(f"missing required field: {'/'.join(keys)}")
    return default


def _first_timestamp_ns(
    row: Mapping[str, Any],
    *keys: str,
    default: int | None = None,
) -> int:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return parse_timestamp_ns(value)
    if default is not None:
        return default
    raise ValueError(f"missing timestamp field: {'/'.join(keys)}")


def _parse_symbols(value: Any) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if isinstance(value, str):
        parts = re.split(r"[,;|]", value)
    elif isinstance(value, Iterable):
        parts = [str(item) for item in value]
    else:
        parts = [str(value)]
    return _dedupe_tuple(_normalize_symbol(part) for part in parts if str(part).strip())


def _dedupe_tuple(values: Iterable[str]) -> tuple[str, ...]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = str(raw).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return tuple(out)


def _normalize_symbol(symbol: str) -> str:
    return symbol.strip().upper().replace("$", "")


def _contains_symbol_token(text: str, symbol: str) -> bool:
    if len(symbol) <= 2:
        return False
    return bool(re.search(rf"(?<![A-Z0-9]){re.escape(symbol)}(?![A-Z0-9])", text))


def _metadata_from_row(row: Mapping[str, Any]) -> dict[str, str]:
    metadata: dict[str, str] = {}
    raw = row.get("metadata")
    if isinstance(raw, Mapping):
        metadata.update((str(k), str(v)) for k, v in raw.items() if v not in (None, ""))
    elif isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            metadata["metadata_raw"] = raw
        else:
            if isinstance(parsed, Mapping):
                metadata.update((str(k), str(v)) for k, v in parsed.items() if v not in (None, ""))
    for key, value in row.items():
        if key.startswith("metadata_") and value not in (None, ""):
            metadata[key.removeprefix("metadata_")] = str(value)
    return metadata


def _stable_event_digest(source: str, headline: str, available_at_ns: int) -> str:
    basis = f"{source}|{_slugify(headline)}|{available_at_ns}"
    return hashlib.sha256(basis.encode()).hexdigest()[:20]


def _slugify(text: str) -> str:
    return _NON_WORD.sub("-", text.strip().lower()).strip("-")


def _event_preference_key(event: NormalizedNewsEvent) -> tuple[int, int, str]:
    return (event.available_at_ns, event.published_at_ns, event.event_id)


def _coerce_epoch_number_to_ns(value: int | float) -> int:
    if isinstance(value, int) and value > 100_000_000_000_000_000:
        return value
    numeric = float(value)
    if numeric > 1e17:
        return int(numeric)
    if numeric > 1e14:
        return int(numeric * NS_PER_MILLISECOND)
    if numeric > 1e11:
        return int(numeric * NS_PER_MILLISECOND)
    return int(numeric * NS_PER_SECOND)


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    lower = int(pos)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = pos - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction
