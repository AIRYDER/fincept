from __future__ import annotations

import csv
from dataclasses import asdict
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from news_impact_model.schema import HistoricalOutcome


RETURN_PREFIXES = ("abnormal_return_", "return_")


def load_historical_outcomes(path: str | Path) -> list[HistoricalOutcome]:
    """Load labeled historical news outcomes from JSONL, JSON, or CSV.

    The loader is intentionally provider-neutral. Vendor-specific adapters
    should normalize into this file contract before training or validation.
    """
    dataset_path = Path(path)
    suffix = dataset_path.suffix.lower()
    if suffix in {".jsonl", ".ndjson"}:
        return _load_jsonl(dataset_path)
    if suffix == ".json":
        return _load_json(dataset_path)
    if suffix == ".csv":
        return _load_csv(dataset_path)
    raise ValueError(f"unsupported historical outcome file type: {dataset_path.suffix}")


def write_historical_outcomes_jsonl(
    path: str | Path,
    outcomes: Iterable[HistoricalOutcome],
) -> None:
    """Write outcomes in the preferred append-friendly training format."""
    dataset_path = Path(path)
    dataset_path.parent.mkdir(parents=True, exist_ok=True)
    with dataset_path.open("w", encoding="utf-8", newline="\n") as handle:
        for outcome in outcomes:
            handle.write(json.dumps(asdict(outcome), sort_keys=True) + "\n")


def _load_jsonl(path: Path) -> list[HistoricalOutcome]:
    outcomes: list[HistoricalOutcome] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            row = json.loads(stripped)
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{line_no}: expected a JSON object")
            outcomes.append(_outcome_from_mapping(row, location=f"{path}:{line_no}"))
    return outcomes


def _load_json(path: Path) -> list[HistoricalOutcome]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and "outcomes" in payload:
        payload = payload["outcomes"]
    if not isinstance(payload, list):
        raise ValueError(f"{path}: expected a JSON array or object with outcomes")
    return [
        _outcome_from_mapping(row, location=f"{path}:{idx}")
        for idx, row in enumerate(payload, start=1)
    ]


def _load_csv(path: Path) -> list[HistoricalOutcome]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return [
            _outcome_from_mapping(row, location=f"{path}:{reader.line_num}")
            for row in reader
        ]


def _outcome_from_mapping(
    row: Mapping[str, Any],
    *,
    location: str,
) -> HistoricalOutcome:
    return HistoricalOutcome(
        event_id=_required_str(row, "event_id", location=location),
        available_at_ns=_required_int(row, "available_at_ns", location=location),
        source=_required_str(row, "source", location=location),
        headline=_required_str(row, "headline", location=location),
        body=_optional_str(row.get("body")),
        symbols=_parse_symbols(row.get("symbols")),
        event_type=_optional_str(row.get("event_type"), default="general"),
        market_regime=_optional_str(row.get("market_regime"), default="unknown"),
        abnormal_returns=_parse_abnormal_returns(row, location=location),
        volatility_impact=_optional_float(row.get("volatility_impact"), default=0.0),
        volume_impact=_optional_float(row.get("volume_impact"), default=0.0),
        metadata=_parse_metadata(row),
    )


def _parse_abnormal_returns(
    row: Mapping[str, Any],
    *,
    location: str,
) -> dict[str, float]:
    returns: dict[str, float] = {}
    raw_returns = row.get("abnormal_returns")
    if raw_returns not in (None, ""):
        parsed = _parse_json_if_needed(raw_returns, location=location)
        if not isinstance(parsed, dict):
            raise ValueError(f"{location}: abnormal_returns must be an object")
        returns.update(
            (str(horizon), float(value))
            for horizon, value in parsed.items()
            if value not in (None, "")
        )

    for key, value in row.items():
        if value in (None, ""):
            continue
        for prefix in RETURN_PREFIXES:
            if key.startswith(prefix):
                horizon = key.removeprefix(prefix)
                if horizon:
                    returns[horizon] = float(value)

    if not returns:
        raise ValueError(f"{location}: at least one abnormal return is required")
    return returns


def _parse_symbols(value: Any) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if isinstance(value, str):
        normalized = value.replace(";", "|").replace(",", "|")
        return tuple(symbol.strip() for symbol in normalized.split("|") if symbol.strip())
    if isinstance(value, Iterable):
        return tuple(str(symbol).strip() for symbol in value if str(symbol).strip())
    return (str(value).strip(),)


def _parse_metadata(row: Mapping[str, Any]) -> dict[str, str]:
    metadata: dict[str, str] = {}
    raw_metadata = row.get("metadata")
    if raw_metadata not in (None, ""):
        parsed = _parse_json_if_needed(raw_metadata, location="metadata")
        if isinstance(parsed, dict):
            metadata.update(
                (str(key), str(value))
                for key, value in parsed.items()
                if value not in (None, "")
            )

    raw_metadata_json = row.get("metadata_json")
    if raw_metadata_json not in (None, ""):
        parsed = _parse_json_if_needed(raw_metadata_json, location="metadata_json")
        if not isinstance(parsed, dict):
            raise ValueError("metadata_json must be a JSON object")
        metadata.update(
            (str(key), str(value))
            for key, value in parsed.items()
            if value not in (None, "")
        )

    for key, value in row.items():
        if key.startswith("metadata_") and key != "metadata_json" and value not in (
            None,
            "",
        ):
            metadata[key.removeprefix("metadata_")] = str(value)
    return metadata


def _parse_json_if_needed(value: Any, *, location: str) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{location}: invalid JSON") from exc
    return value


def _required_str(
    row: Mapping[str, Any],
    key: str,
    *,
    location: str,
) -> str:
    value = row.get(key)
    if value in (None, ""):
        raise ValueError(f"{location}: missing required field {key!r}")
    return str(value)


def _required_int(
    row: Mapping[str, Any],
    key: str,
    *,
    location: str,
) -> int:
    value = row.get(key)
    if value in (None, ""):
        raise ValueError(f"{location}: missing required field {key!r}")
    return int(value)


def _optional_str(value: Any, *, default: str = "") -> str:
    if value in (None, ""):
        return default
    return str(value)


def _optional_float(value: Any, *, default: float) -> float:
    if value in (None, ""):
        return default
    return float(value)
