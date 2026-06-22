"""
fincept_db.provider_receipts — provider evidence receipts with freshness (TASK-0205).

A provider evidence receipt proves that a provider delivered data at a
specific time, with a specific row count, and a specific freshness — WITHOUT
leaking API keys, account identifiers, or raw private URLs.

Design:
- ``ProviderEvidenceReceipt`` is a frozen dataclass with provider name,
  source, dataset, symbol, timestamps, row count, request hash, redacted
  request, freshness status, and redaction metadata.
- ``build_evidence_receipt(...)`` constructs a receipt from raw provider
  data, automatically redacting the request dict.
- ``freshness_from_age_sec(...)`` classifies data freshness as fresh / stale /
  degraded / unknown based on age thresholds.
- ``to_dict()`` serializes the receipt to a JSON-safe dict for API responses
  and storage.

File-disjoint from all active builders. New module in fincept-db.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

from .evidence_redaction import redact_dict

# --------------------------------------------------------------------------- #
# Freshness thresholds (seconds)                                               #
# --------------------------------------------------------------------------- #

DEFAULT_FRESH_THRESHOLD_SEC = 5  # < 5s = fresh
DEFAULT_STALE_THRESHOLD_SEC = 60  # 5-60s = stale
DEFAULT_DEGRADED_THRESHOLD_SEC = 60  # >= 60s = degraded


# --------------------------------------------------------------------------- #
# Freshness status                                                             #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ProviderFreshnessStatus:
    """Freshness classification for a provider's data.

    - ``status``: "fresh" | "stale" | "degraded" | "unknown"
    - ``age_sec``: age of the data in seconds (None if unknown)
    - ``provider``: provider name for context
    """

    status: str  # "fresh" | "stale" | "degraded" | "unknown"
    age_sec: int | None
    provider: str


def freshness_from_age_sec(
    *,
    age_sec: int | None,
    provider: str,
    fresh_threshold_sec: int = DEFAULT_FRESH_THRESHOLD_SEC,
    stale_threshold_sec: int = DEFAULT_STALE_THRESHOLD_SEC,
    degraded_threshold_sec: int = DEFAULT_DEGRADED_THRESHOLD_SEC,
) -> ProviderFreshnessStatus:
    """Classify data freshness based on age in seconds.

    Args:
        age_sec: age of the data in seconds, or None if unknown.
        provider: provider name (for context in the status object).
        fresh_threshold_sec: below this = fresh.
        stale_threshold_sec: below this = stale (but acceptable).
        degraded_threshold_sec: below this = degraded; above = also degraded.

    Returns:
        ``ProviderFreshnessStatus`` with the classification.
    """
    if age_sec is None:
        return ProviderFreshnessStatus(status="unknown", age_sec=None, provider=provider)

    if age_sec < fresh_threshold_sec:
        return ProviderFreshnessStatus(status="fresh", age_sec=age_sec, provider=provider)
    if age_sec < stale_threshold_sec:
        return ProviderFreshnessStatus(status="stale", age_sec=age_sec, provider=provider)
    return ProviderFreshnessStatus(status="degraded", age_sec=age_sec, provider=provider)


# --------------------------------------------------------------------------- #
# Evidence receipt                                                             #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ProviderEvidenceReceipt:
    """A provider evidence receipt with redacted request and freshness.

    This receipt proves data freshness without leaking secrets. The
    ``request`` field is redacted before the receipt is created; the
    ``redaction_count`` and ``redaction_patterns`` fields record what was
    redacted for audit purposes.
    """

    provider: str
    source: str
    dataset: str
    symbol: str | None
    ts_event: int  # unix seconds (when the data was generated)
    ts_received: int  # unix seconds (when Fincept received it)
    row_count: int
    request_hash: str  # hash of the original (unredacted) request
    request: dict[str, Any]  # redacted request dict
    ok: bool
    error_type: str | None
    freshness: ProviderFreshnessStatus
    redaction_count: int
    redaction_patterns: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict for API responses and storage."""
        d = asdict(self)
        # Flatten freshness for cleaner API output.
        d["freshness_status"] = self.freshness.status
        d["freshness_age_sec"] = self.freshness.age_sec
        d.pop("freshness", None)
        return d


def build_evidence_receipt(
    *,
    provider: str,
    source: str,
    dataset: str,
    symbol: str | None,
    ts_event: int,
    ts_received: int | None,
    row_count: int,
    request_hash: str,
    request: dict[str, Any],
    ok: bool,
    error_type: str | None = None,
    fresh_threshold_sec: int = DEFAULT_FRESH_THRESHOLD_SEC,
    stale_threshold_sec: int = DEFAULT_STALE_THRESHOLD_SEC,
    degraded_threshold_sec: int = DEFAULT_DEGRADED_THRESHOLD_SEC,
) -> ProviderEvidenceReceipt:
    """Build a provider evidence receipt with automatic redaction.

    The ``request`` dict is redacted before the receipt is created. The
    original (unredacted) request is never stored in the receipt.

    Args:
        provider: provider name (e.g. "binance", "polygon", "alpaca").
        source: source type (e.g. "websocket", "rest", "exa").
        dataset: dataset name (e.g. "bars", "aggs", "research_brief").
        symbol: trading symbol, or None for non-symbol datasets.
        ts_event: unix timestamp when the data was generated (provider side).
        ts_received: unix timestamp when Fincept received it. If None,
            defaults to the current time.
        row_count: number of rows in the response.
        request_hash: hash of the original (unredacted) request, for
            deduplication and audit.
        request: the original request dict (will be redacted).
        ok: whether the request succeeded.
        error_type: error type if the request failed, None otherwise.
        fresh_threshold_sec: freshness threshold (see ``freshness_from_age_sec``).
        stale_threshold_sec: stale threshold.
        degraded_threshold_sec: degraded threshold.

    Returns:
        ``ProviderEvidenceReceipt`` with redacted request and freshness.
    """
    ts_recv = ts_received if ts_received is not None else int(time.time())
    age_sec = ts_recv - ts_event if ts_event > 0 else None

    freshness = freshness_from_age_sec(
        age_sec=age_sec,
        provider=provider,
        fresh_threshold_sec=fresh_threshold_sec,
        stale_threshold_sec=stale_threshold_sec,
        degraded_threshold_sec=degraded_threshold_sec,
    )

    redaction = redact_dict(request)

    return ProviderEvidenceReceipt(
        provider=provider,
        source=source,
        dataset=dataset,
        symbol=symbol,
        ts_event=ts_event,
        ts_received=ts_recv,
        row_count=row_count,
        request_hash=request_hash,
        request=redaction.redacted,
        ok=ok,
        error_type=error_type,
        freshness=freshness,
        redaction_count=redaction.redaction_count,
        redaction_patterns=redaction.patterns_matched,
    )
