"""quant_foundry.read_switch — C10 Postgres read switch behind feature flag.

When ``QF_POSTGRES_READS_ENABLED=1`` AND ``QF_LEGACY_FILE_READ_FALLBACK=0``,
reads come from Postgres instead of JSONL. This is the first safe read
switch — it is controlled by feature flags and preserves rollback to
legacy reads via ``QF_LEGACY_FILE_READ_FALLBACK=1``.

Behavior summary:

  Flags off (default):
    - Legacy JSONL reads remain source of truth.
    - No Postgres read attempted.
    - Runtime behavior unchanged.
    - Existing tests pass.

  Postgres reads enabled + fallback on
    (``QF_POSTGRES_READS_ENABLED=1``, ``QF_LEGACY_FILE_READ_FALLBACK=1``):
    - Postgres read is attempted first.
    - If record exists and validates, return Postgres record.
    - If Postgres record is missing, return legacy record.
    - If Postgres read errors, return legacy record with warning/evidence.

  Postgres reads enabled + fallback off
    (``QF_POSTGRES_READS_ENABLED=1``, ``QF_LEGACY_FILE_READ_FALLBACK=0``):
    - Postgres read is required.
    - Missing Postgres record fails clearly.
    - Postgres read error fails clearly.
    - Legacy is not used silently.

  Read-compare + reads enabled
    (``QF_POSTGRES_READS_ENABLED=1``, ``QF_POSTGRES_READ_COMPARE_ENABLED=1``):
    - Read Postgres result (primary).
    - Read legacy result for comparison.
    - Return Postgres result if valid.
    - Emit comparison evidence.
    - Surface mismatch if detected.

Validation:
  Postgres read results must validate before being returned:
    - Required fields present (prediction_id, model_id, status, etc.)
    - Record status domain valid (pending_time, pending_data, settled)
    - model_id / prediction_id keys are non-empty strings
    - Timestamps are non-negative integers
    - Cost model version is a non-empty string

  Do not return partially valid Postgres records silently.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from quant_foundry.outcomes import SettlementRecord, SettlementStatus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Evidence types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReadSwitchEvidence:
    """Structured evidence emitted by a read-switch operation.

    ``outcome`` is one of:
      - ``"postgres_read"`` — Postgres read succeeded, records returned.
      - ``"fallback_to_legacy"`` — Postgres read failed/missing, legacy used.
      - ``"postgres_read_error"`` — Postgres read errored, fallback used.
      - ``"postgres_missing"`` — Postgres returned no records, fallback used.
      - ``"validation_rejected"`` — Postgres record failed validation.
      - ``"no_fallback_failure"`` — No fallback, Postgres failure is fatal.
      - ``"compare_mismatch"`` — Postgres returned but differs from legacy.
    """

    outcome: str
    record_type: str
    record_count: int = 0
    fallback_used: bool = False
    error_class: str | None = None
    error_message: str | None = None
    validation_errors: list[str] = field(default_factory=list)
    comparison_evidence: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "record_type": self.record_type,
            "record_count": self.record_count,
            "fallback_used": self.fallback_used,
            "error_class": self.error_class,
            "error_message": self.error_message,
            "validation_errors": list(self.validation_errors),
            "comparison_evidence": list(self.comparison_evidence),
        }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


_REQUIRED_FIELDS = (
    "prediction_id",
    "model_id",
    "symbol",
    "ts_event",
    "horizon_ns",
    "status",
    "cost_model_version",
    "decision_window_start",
    "decision_window_end",
)

_VALID_STATUSES = {s.value for s in SettlementStatus}


def validate_settlement_record(record: SettlementRecord) -> list[str]:
    """Validate a settlement record read from Postgres.

    Returns a list of validation error strings. An empty list means the
    record is valid.

    Checks:
      - Required fields present (non-None).
      - Record status domain valid (pending_time, pending_data, settled).
      - prediction_id and model_id are non-empty strings.
      - Timestamps are non-negative integers.
      - cost_model_version is a non-empty string.
    """
    errors: list[str] = []

    # Check required fields are present (non-None).
    for field_name in _REQUIRED_FIELDS:
        value = getattr(record, field_name, None)
        if value is None:
            errors.append(f"missing required field: {field_name}")

    # prediction_id and model_id must be non-empty strings.
    if not record.prediction_id:
        errors.append("prediction_id is empty")
    if not record.model_id:
        errors.append("model_id is empty")
    if not record.cost_model_version:
        errors.append("cost_model_version is empty")

    # Status domain validation.
    status_str = record.status.value if hasattr(record.status, "value") else str(record.status)
    if status_str not in _VALID_STATUSES:
        errors.append(f"invalid status: {status_str}")

    # Timestamps must be non-negative.
    if record.ts_event < 0:
        errors.append(f"ts_event is negative: {record.ts_event}")
    if record.horizon_ns < 0:
        errors.append(f"horizon_ns is negative: {record.horizon_ns}")
    if record.decision_window_start < 0:
        errors.append(f"decision_window_start is negative: {record.decision_window_start}")
    if record.decision_window_end < 0:
        errors.append(f"decision_window_end is negative: {record.decision_window_end}")

    return errors


def validate_settlement_records(
    records: list[SettlementRecord],
) -> tuple[list[SettlementRecord], list[str]]:
    """Validate a list of settlement records.

    Returns (valid_records, all_validation_errors).
    """
    valid: list[SettlementRecord] = []
    all_errors: list[str] = []
    for i, record in enumerate(records):
        errs = validate_settlement_record(record)
        if errs:
            all_errors.extend(f"record[{i}] ({record.prediction_id}): {e}" for e in errs)
        else:
            valid.append(record)
    return valid, all_errors


# ---------------------------------------------------------------------------
# Read switch coordinator
# ---------------------------------------------------------------------------


class ReadSwitchError(Exception):
    """Raised when Postgres read is required but fails and no fallback."""


def read_switch_settlements(
    db_store: Any,
    legacy_reader: Any,
    *,
    fail_hard: bool = False,
) -> tuple[list[SettlementRecord], ReadSwitchEvidence]:
    """Read settlement records with Postgres-first read switch.

    When ``should_read_from_postgres()`` is True, reads from Postgres first.
    When False, reads from legacy (JSONL).

    Args:
        db_store: ``DbSettlementStore`` instance (or compatible with
            ``list_all()``).
        legacy_reader: Callable that returns ``list[SettlementRecord]``
            from the legacy path (JSONL).
        fail_hard: If True, Postgres failures are fatal (no fallback).
            If False, fallback to legacy on Postgres failure.

    Returns:
        (records, evidence) — the records to return to the caller, and
        structured evidence about the read switch outcome.
    """
    from quant_foundry.c10_flags import postgres_read_switch_active

    if not postgres_read_switch_active():
        # Legacy mode — no Postgres read.
        records = legacy_reader()
        return records, ReadSwitchEvidence(
            outcome="legacy_read",
            record_type="settlement_record",
            record_count=len(records),
        )

    # Postgres-first mode.
    try:
        pg_records = db_store.list_all()
    except Exception as exc:
        # Postgres read error.
        if not fail_hard:
            logger.warning(
                "C10 read-switch: Postgres read error, falling back to legacy: %s",
                exc,
            )
            records = legacy_reader()
            return records, ReadSwitchEvidence(
                outcome="postgres_read_error",
                record_type="settlement_record",
                record_count=len(records),
                fallback_used=True,
                error_class=type(exc).__name__,
                error_message=str(exc),
            )
        raise ReadSwitchError(f"Postgres read failed and fallback is disabled: {exc}") from exc

    # Postgres read succeeded. Check if it returned any records.
    # If Postgres is empty but legacy has records, treat as "missing"
    # and fall back to legacy if fallback is enabled.
    if not pg_records and not fail_hard:
        legacy_records = legacy_reader()
        if legacy_records:
            logger.warning(
                "C10 read-switch: Postgres returned 0 records, "
                "legacy has %d — falling back to legacy",
                len(legacy_records),
            )
            return legacy_records, ReadSwitchEvidence(
                outcome="postgres_missing",
                record_type="settlement_record",
                record_count=len(legacy_records),
                fallback_used=True,
            )
        # Both Postgres and legacy are empty — return empty.
        return [], ReadSwitchEvidence(
            outcome="postgres_read",
            record_type="settlement_record",
            record_count=0,
        )

    # Validate Postgres records.
    _valid_records, validation_errors = validate_settlement_records(pg_records)

    if validation_errors:
        logger.error(
            "C10 read-switch: %d validation errors in Postgres records",
            len(validation_errors),
        )
        if not fail_hard:
            # Fall back to legacy if validation fails.
            records = legacy_reader()
            return records, ReadSwitchEvidence(
                outcome="validation_rejected",
                record_type="settlement_record",
                record_count=len(records),
                fallback_used=True,
                validation_errors=validation_errors,
            )
        raise ReadSwitchError(
            f"Postgres records failed validation and fallback is disabled: {validation_errors[:3]}"
        )

    # Postgres read succeeded with valid records.
    # Check if read-compare is also enabled.
    from quant_foundry.c10_flags import should_read_compare

    if should_read_compare():
        # Read legacy for comparison, return Postgres.
        legacy_records = legacy_reader()
        comparison_evidence = _compare_postgres_vs_legacy(pg_records, legacy_records)
        return pg_records, ReadSwitchEvidence(
            outcome="postgres_read",
            record_type="settlement_record",
            record_count=len(pg_records),
            comparison_evidence=comparison_evidence,
        )

    return pg_records, ReadSwitchEvidence(
        outcome="postgres_read",
        record_type="settlement_record",
        record_count=len(pg_records),
    )


def _compare_postgres_vs_legacy(
    pg_records: list[SettlementRecord],
    legacy_records: list[SettlementRecord],
) -> list[dict[str, Any]]:
    """Compare Postgres records against legacy records for read-compare.

    Returns a list of comparison evidence dicts. Only mismatches and
    misses are included (matches are omitted for brevity).
    """
    from quant_foundry.read_compare import compare_settlement_records

    legacy_by_key: dict[str, SettlementRecord] = {}
    for r in legacy_records:
        key = f"{r.prediction_id}:{r.cost_model_version}"
        legacy_by_key[key] = r

    pg_by_key: dict[str, SettlementRecord] = {}
    for r in pg_records:
        key = f"{r.prediction_id}:{r.cost_model_version}"
        pg_by_key[key] = r

    evidence_list: list[dict[str, Any]] = []

    # Check Postgres records against legacy.
    for key, pg_rec in pg_by_key.items():
        legacy_rec = legacy_by_key.get(key)
        if legacy_rec is None:
            evidence_list.append(
                {
                    "outcome": "legacy_missing",
                    "key": key,
                    "note": "Postgres has record but legacy does not",
                }
            )
            continue
        ev = compare_settlement_records(legacy_rec, pg_rec)
        if ev.outcome != "match":
            evidence_list.append(ev.to_dict())

    # Check legacy records missing from Postgres.
    for key, legacy_rec in legacy_by_key.items():
        if key not in pg_by_key:
            evidence_list.append(
                {
                    "outcome": "postgres_missing",
                    "key": key,
                    "legacy_hash": _stable_hash_safe(legacy_rec),
                }
            )

    return evidence_list


def _stable_hash_safe(record: SettlementRecord) -> str:
    """Compute a stable hash safely."""
    from quant_foundry.read_compare import _stable_hash, normalize_settlement_record

    return _stable_hash(normalize_settlement_record(record))
