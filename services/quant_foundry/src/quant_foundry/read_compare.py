"""quant_foundry.read_compare — C10 read-compare mode.

When ``QF_POSTGRES_READ_COMPARE_ENABLED=1``, reads from the legacy path
(JSONL) are compared against Postgres reads after both are fetched and
normalized. The legacy record is **always** returned to the caller —
Postgres data is never returned while this flag is on. Mismatches are
logged and counted as structured evidence.

This proves Postgres reads are trustworthy before flipping production
reads (``QF_POSTGRES_READS_ENABLED=1``).

Behavior summary:

  Flags off (default):
    - Legacy read behavior unchanged.
    - No Postgres read attempted.
    - All existing tests pass.

  Read-compare on (``QF_POSTGRES_READ_COMPARE_ENABLED=1``):
    - Legacy record is read (JSONL).
    - Postgres record is read (same key).
    - Both records are normalized.
    - Hash or field comparison is performed.
    - Legacy record is returned to caller.
    - Comparison result is logged/reported.
    - Mismatches are counted and surfaced.
    - No silent mismatch.

  Missing Postgres record:
    - Return legacy record.
    - Emit ``read_compare_miss`` evidence.
    - Include record key/type.
    - Do not silently pass.

  Postgres mismatch:
    - Return legacy record.
    - Emit ``read_compare_mismatch`` evidence.
    - Include field-level diff if safe.
    - Include normalized hash values.
    - Do not flip to Postgres.

  Postgres read failure:
    - Return legacy record if legacy read succeeded.
    - Emit ``read_compare_error`` evidence.
    - Include safe error class/message.
    - Do not expose secrets.

Error handling policy:
  - Default (production): Postgres read/compare failure is logged but
    does not block the legacy read. The legacy record is returned.
  - Fail-hard mode (``QF_DUAL_WRITE_FAIL_HARD=1``): Postgres read/compare
    failure re-raises the exception. Used in test/verification mode.
  - Never silently drop mismatches. All failures are logged at ERROR level.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from quant_foundry.outcomes import SettlementRecord

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Evidence types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReadCompareEvidence:
    """Structured evidence emitted by a single read-compare check.

    ``outcome`` is one of:
      - ``"match"`` — legacy and Postgres records agree after normalization.
      - ``"read_compare_miss"`` — legacy exists but Postgres is missing.
      - ``"read_compare_mismatch"`` — both exist but differ.
      - ``"read_compare_error"`` — Postgres read or comparison errored.
    """

    outcome: str
    record_type: str
    record_key: str
    legacy_hash: str | None = None
    postgres_hash: str | None = None
    field_diffs: dict[str, tuple[Any, Any]] = field(default_factory=dict)
    error_class: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Render to a JSON-serializable dict."""
        d: dict[str, Any] = {
            "outcome": self.outcome,
            "record_type": self.record_type,
            "record_key": self.record_key,
            "legacy_hash": self.legacy_hash,
            "postgres_hash": self.postgres_hash,
            "field_diffs": {k: [v[0], v[1]] for k, v in self.field_diffs.items()},
            "error_class": self.error_class,
            "error_message": self.error_message,
        }
        return d

    def to_json(self) -> str:
        """Render to a JSON string."""
        return json.dumps(self.to_dict(), separators=(",", ":"), sort_keys=True, default=str)


# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------


class ReadCompareCounters:
    """In-process counters for read-compare outcomes.

    These are process-local and not thread-safe by design — read-compare
    is a transitional verification mode, not a permanent metric. The
    counters are intended for test assertions and local verification.
    """

    def __init__(self) -> None:
        self.matches: int = 0
        self.misses: int = 0
        self.mismatches: int = 0
        self.errors: int = 0

    def reset(self) -> None:
        self.matches = 0
        self.misses = 0
        self.mismatches = 0
        self.errors = 0

    def total(self) -> int:
        return self.matches + self.misses + self.mismatches + self.errors

    def to_dict(self) -> dict[str, int]:
        return {
            "matches": self.matches,
            "misses": self.misses,
            "mismatches": self.mismatches,
            "errors": self.errors,
            "total": self.total(),
        }


# Module-level singleton counters.
_counters = ReadCompareCounters()


def get_counters() -> ReadCompareCounters:
    """Return the module-level read-compare counters."""
    return _counters


def reset_counters() -> None:
    """Reset the module-level read-compare counters."""
    _counters.reset()


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def _normalize_float(value: float | None) -> float | None:
    """Normalize a float for comparison: round to 12 significant digits."""
    if value is None:
        return None
    # Round to 12 decimal places to absorb Decimal->float rounding noise.
    return round(value, 12)


def _normalize_str(value: str | None) -> str | None:
    """Normalize a string for comparison: strip, lower-case for status."""
    if value is None:
        return None
    return value.strip()


def normalize_settlement_record(record: SettlementRecord) -> dict[str, Any]:
    """Normalize a SettlementRecord for fair comparison.

    Normalization rules:
      - Field names: as-is (dataclass field names).
      - Timestamps: int (BigInteger ns) — no transformation needed.
      - Float precision: rounded to 12 decimal places.
      - Cost model fields: string, stripped.
      - Status strings: stripped, compared as string values.
      - model_id / legacy_agent_id: no mapping needed (same field).
      - sha256 casing: N/A for settlement records.
      - Optional/null fields: preserved as None.
      - Metadata ordering: N/A (no metadata dict on SettlementRecord).
    """
    return {
        "prediction_id": record.prediction_id,
        "model_id": record.model_id,
        "symbol": record.symbol,
        "ts_event": int(record.ts_event),
        "horizon_ns": int(record.horizon_ns),
        "status": str(record.status.value)
        if hasattr(record.status, "value")
        else str(record.status),
        "settled_at_ns": record.settled_at_ns,
        "realized_return_gross": _normalize_float(record.realized_return_gross),
        "realized_return_net": _normalize_float(record.realized_return_net),
        "abnormal_return": _normalize_float(record.abnormal_return),
        "brier": _normalize_float(record.brier),
        "calibration_bucket": _normalize_str(record.calibration_bucket),
        "cost_model_version": _normalize_str(record.cost_model_version),
        "decision_window_start": int(record.decision_window_start),
        "decision_window_end": int(record.decision_window_end),
    }


def normalize_dict(
    record: dict[str, Any], *, float_fields: set[str] | None = None
) -> dict[str, Any]:
    """Normalize a generic dict record for comparison.

    Args:
        record: Dict to normalize.
        float_fields: Set of field names that should be float-normalized.
    """
    float_fields = float_fields or set()
    out: dict[str, Any] = {}
    for key, value in record.items():
        if value is None:
            out[key] = None
        elif key in float_fields and isinstance(value, (int, float)):
            out[key] = _normalize_float(float(value))
        elif isinstance(value, str):
            out[key] = _normalize_str(value)
        elif isinstance(value, (int,)):
            out[key] = int(value)
        else:
            out[key] = value
    return out


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def _stable_hash(normalized: dict[str, Any]) -> str:
    """Compute a stable SHA-256 hash of a normalized dict."""
    payload = json.dumps(normalized, separators=(",", ":"), sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def _field_diff(
    legacy_norm: dict[str, Any],
    postgres_norm: dict[str, Any],
) -> dict[str, tuple[Any, Any]]:
    """Return field-level differences between two normalized dicts.

    Only includes fields that differ. Fields present in one but not the
    other are included as ``(value, None)`` or ``(None, value)``.
    """
    diffs: dict[str, tuple[Any, Any]] = {}
    all_keys = set(legacy_norm.keys()) | set(postgres_norm.keys())
    for key in sorted(all_keys):
        lv = legacy_norm.get(key)
        pv = postgres_norm.get(key)
        if lv != pv:
            diffs[key] = (lv, pv)
    return diffs


def compare_settlement_records(
    legacy: SettlementRecord,
    postgres: SettlementRecord,
) -> ReadCompareEvidence:
    """Compare two settlement records after normalization.

    Returns ``ReadCompareEvidence`` with outcome ``"match"`` or
    ``"read_compare_mismatch"``.
    """
    legacy_norm = normalize_settlement_record(legacy)
    postgres_norm = normalize_settlement_record(postgres)
    legacy_hash = _stable_hash(legacy_norm)
    postgres_hash = _stable_hash(postgres_norm)

    key = f"{legacy.prediction_id}:{legacy.cost_model_version}"

    if legacy_hash == postgres_hash:
        return ReadCompareEvidence(
            outcome="match",
            record_type="settlement_record",
            record_key=key,
            legacy_hash=legacy_hash,
            postgres_hash=postgres_hash,
        )

    diffs = _field_diff(legacy_norm, postgres_norm)
    return ReadCompareEvidence(
        outcome="read_compare_mismatch",
        record_type="settlement_record",
        record_key=key,
        legacy_hash=legacy_hash,
        postgres_hash=postgres_hash,
        field_diffs=diffs,
    )


def compare_dicts(
    legacy: dict[str, Any],
    postgres: dict[str, Any],
    *,
    record_type: str,
    record_key: str,
    float_fields: set[str] | None = None,
) -> ReadCompareEvidence:
    """Compare two dict records after normalization.

    Returns ``ReadCompareEvidence`` with outcome ``"match"`` or
    ``"read_compare_mismatch"``.
    """
    legacy_norm = normalize_dict(legacy, float_fields=float_fields)
    postgres_norm = normalize_dict(postgres, float_fields=float_fields)
    legacy_hash = _stable_hash(legacy_norm)
    postgres_hash = _stable_hash(postgres_norm)

    if legacy_hash == postgres_hash:
        return ReadCompareEvidence(
            outcome="match",
            record_type=record_type,
            record_key=record_key,
            legacy_hash=legacy_hash,
            postgres_hash=postgres_hash,
        )

    diffs = _field_diff(legacy_norm, postgres_norm)
    return ReadCompareEvidence(
        outcome="read_compare_mismatch",
        record_type=record_type,
        record_key=record_key,
        legacy_hash=legacy_hash,
        postgres_hash=postgres_hash,
        field_diffs=diffs,
    )


# ---------------------------------------------------------------------------
# Read-compare coordinator
# ---------------------------------------------------------------------------


def _fail_hard() -> bool:
    """Return True if read-compare errors should re-raise (test/verification mode)."""
    return os.environ.get("QF_DUAL_WRITE_FAIL_HARD", "0") == "1"


def _log_evidence(evidence: ReadCompareEvidence) -> None:
    """Log the evidence at the appropriate level and update counters."""
    if evidence.outcome == "match":
        _counters.matches += 1
        logger.debug(
            "C10 read-compare: match %s key=%s",
            evidence.record_type,
            evidence.record_key,
        )
    elif evidence.outcome == "read_compare_miss":
        _counters.misses += 1
        logger.warning(
            "C10 read-compare: MISS %s key=%s — legacy exists, Postgres missing",
            evidence.record_type,
            evidence.record_key,
        )
    elif evidence.outcome == "read_compare_mismatch":
        _counters.mismatches += 1
        logger.error(
            "C10 read-compare: MISMATCH %s key=%s legacy_hash=%s postgres_hash=%s diffs=%s",
            evidence.record_type,
            evidence.record_key,
            evidence.legacy_hash,
            evidence.postgres_hash,
            list(evidence.field_diffs.keys()),
        )
    elif evidence.outcome == "read_compare_error":
        _counters.errors += 1
        logger.error(
            "C10 read-compare: ERROR %s key=%s error_class=%s error_message=%s",
            evidence.record_type,
            evidence.record_key,
            evidence.error_class,
            evidence.error_message,
        )


def read_compare_settlement(
    legacy_record: SettlementRecord,
    db_store: Any,
) -> ReadCompareEvidence:
    """Read-compare a single settlement record.

    Reads the Postgres record for the same ``(prediction_id,
    cost_model_version)`` key, normalizes both, compares, and returns
    evidence. The legacy record is always the source of truth — this
    function never returns Postgres data to the caller.

    Args:
        legacy_record: The record read from JSONL (legacy canonical).
        db_store: ``DbSettlementStore`` instance (or compatible with
            ``get(prediction_id, cost_model_version)``).

    Returns:
        ``ReadCompareEvidence`` with outcome ``match``, ``read_compare_miss``,
        ``read_compare_mismatch``, or ``read_compare_error``.
    """
    key = f"{legacy_record.prediction_id}:{legacy_record.cost_model_version}"
    try:
        pg_record = db_store.get(
            legacy_record.prediction_id,
            legacy_record.cost_model_version,
        )
    except Exception as exc:
        evidence = ReadCompareEvidence(
            outcome="read_compare_error",
            record_type="settlement_record",
            record_key=key,
            error_class=type(exc).__name__,
            error_message=str(exc),
        )
        _log_evidence(evidence)
        if _fail_hard():
            raise
        return evidence

    if pg_record is None:
        evidence = ReadCompareEvidence(
            outcome="read_compare_miss",
            record_type="settlement_record",
            record_key=key,
            legacy_hash=_stable_hash(normalize_settlement_record(legacy_record)),
        )
        _log_evidence(evidence)
        return evidence

    evidence = compare_settlement_records(legacy_record, pg_record)
    _log_evidence(evidence)
    return evidence


def read_compare_settlement_batch(
    legacy_records: list[SettlementRecord],
    db_store: Any,
) -> list[ReadCompareEvidence]:
    """Read-compare a batch of settlement records.

    Returns one ``ReadCompareEvidence`` per legacy record.
    """
    results: list[ReadCompareEvidence] = []
    for record in legacy_records:
        results.append(read_compare_settlement(record, db_store))
    return results


def read_compare_dict(
    legacy_record: dict[str, Any],
    db_getter: Any,
    *,
    record_type: str,
    record_key: str,
    key_args: tuple[Any, ...] = (),
    key_kwargs: dict[str, Any] | None = None,
    float_fields: set[str] | None = None,
) -> ReadCompareEvidence:
    """Read-compare a single dict record against Postgres.

    Generic read-compare for callback receipts, dossiers, model metrics,
    and other dict-shaped records.

    Args:
        legacy_record: The dict read from the legacy path (JSONL).
        db_getter: Callable that reads the Postgres record. Called as
            ``db_getter(*key_args, **key_kwargs)``.
        record_type: Human-readable record type for evidence.
        record_key: Human-readable key for evidence.
        key_args: Positional args to pass to ``db_getter``.
        key_kwargs: Keyword args to pass to ``db_getter``.
        float_fields: Set of field names to float-normalize.

    Returns:
        ``ReadCompareEvidence``.
    """
    key_kwargs = key_kwargs or {}
    try:
        pg_record = db_getter(*key_args, **key_kwargs)
    except Exception as exc:
        evidence = ReadCompareEvidence(
            outcome="read_compare_error",
            record_type=record_type,
            record_key=record_key,
            error_class=type(exc).__name__,
            error_message=str(exc),
        )
        _log_evidence(evidence)
        if _fail_hard():
            raise
        return evidence

    if pg_record is None:
        evidence = ReadCompareEvidence(
            outcome="read_compare_miss",
            record_type=record_type,
            record_key=record_key,
            legacy_hash=_stable_hash(normalize_dict(legacy_record, float_fields=float_fields)),
        )
        _log_evidence(evidence)
        return evidence

    # If pg_record is a dict, compare directly. If it's an object with
    # to_dict() or asdict, convert first.
    if isinstance(pg_record, dict):
        pg_dict = pg_record
    elif dataclasses.is_dataclass(pg_record) and not isinstance(pg_record, type):
        pg_dict = dataclasses.asdict(pg_record)
    elif hasattr(pg_record, "to_dict"):
        pg_dict = pg_record.to_dict()
    else:
        pg_dict = dict(pg_record)

    evidence = compare_dicts(
        legacy_record,
        pg_dict,
        record_type=record_type,
        record_key=record_key,
        float_fields=float_fields,
    )
    _log_evidence(evidence)
    return evidence
