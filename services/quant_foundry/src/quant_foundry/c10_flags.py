"""quant_foundry.c10_flags — feature flags for the C10 Postgres sink flip.

All flags default to **safe legacy mode** — no Postgres writes, no Postgres
reads, dual-write off, legacy file read fallback on. This means C10 schema
and repository code can ship without changing runtime behavior.

Flag defaults (see reports/c10-postgres-sink-flip/C10_POSTGRES_SINK_PREFLIGHT_DESIGN.md):

  QF_POSTGRES_SINK_ENABLED=0
      When 1, settlement records are also written to Postgres (dual-write).
      Default 0 — JSONL only.

  QF_POSTGRES_READS_ENABLED=0
      When 1, reads come from Postgres instead of JSONL.
      Default 0 — JSONL reads.

  QF_DUAL_WRITE_SETTLEMENTS=0
      When 1, JSONL writes continue alongside Postgres writes.
      Set to 0 to retire JSONL writes (only after reads are flipped and
      proven green for 7+ days).
      Default 0 — dual-write not active (sink is off by default).

  QF_LEGACY_FILE_READ_FALLBACK=1
      When 1, reads always come from JSONL regardless of
      QF_POSTGRES_READS_ENABLED. Emergency rollback flag.
      Default 1 — legacy reads are the safe default.

  QF_POSTGRES_READ_COMPARE_ENABLED=0
      When 1, reads from the legacy path (JSONL) are compared against
      Postgres reads after both are fetched and normalized. The legacy
      record is always returned to the caller — Postgres data is never
      returned while this flag is on. Mismatches are logged and counted.
      Default 0 — no comparison.

  QF_DUAL_WRITE_FAIL_HARD=0
      When 1, dual-write or read-compare errors are re-raised to the
      caller. When 0 (default), errors are logged at ERROR level but
      do not interrupt the legacy code path.

These flags are orthogonal to the existing ``SETTLEMENTS_USE_PATH_B`` flag,
which controls Path A vs Path B settlement *computation*. The ``QF_*`` flags
control the *storage backend*.
"""

from __future__ import annotations

import os


def _flag(name: str, default: str) -> bool:
    """Return True if the env var is set to a truthy value (1, true, yes)."""
    val = os.getenv(name, default)
    return val.strip().lower() in {"1", "true", "yes", "on"}


def postgres_sink_enabled() -> bool:
    """Return True if the Postgres settlement sink is enabled (dual-write)."""
    return _flag("QF_POSTGRES_SINK_ENABLED", "0")


def postgres_reads_enabled() -> bool:
    """Return True if reads should come from Postgres instead of JSONL."""
    return _flag("QF_POSTGRES_READS_ENABLED", "0")


def dual_write_settlements() -> bool:
    """Return True if JSONL writes continue alongside Postgres writes.

    When ``QF_POSTGRES_SINK_ENABLED=0`` (default), this is irrelevant —
    no Postgres writes happen. When the sink is enabled, this flag controls
    whether JSONL writes continue (dual-write) or stop (Postgres only).
    """
    return _flag("QF_DUAL_WRITE_SETTLEMENTS", "0")


def legacy_file_read_fallback() -> bool:
    """Return True if reads should fall back to JSONL regardless of other flags.

    This is the emergency rollback flag. When True, reads always come from
    JSONL files, even if ``QF_POSTGRES_READS_ENABLED=1``.
    """
    return _flag("QF_LEGACY_FILE_READ_FALLBACK", "1")


def postgres_read_compare_enabled() -> bool:
    """Return True if read-compare mode is active.

    When True, the legacy read result is compared against the Postgres read
    result after normalization. The legacy record is always returned to the
    caller — Postgres data is never returned while this flag is on.
    Mismatches are logged and counted.
    """
    return _flag("QF_POSTGRES_READ_COMPARE_ENABLED", "0")


def dual_write_fail_hard() -> bool:
    """Return True if dual-write / read-compare errors should be re-raised.

    When True, errors from the Postgres side (write or read-compare) are
    re-raised to the caller. When False (default), errors are logged at
    ERROR level but do not interrupt the legacy code path.
    """
    return _flag("QF_DUAL_WRITE_FAIL_HARD", "0")


def should_write_to_postgres() -> bool:
    """Return True if settlement records should be written to Postgres.

    True when ``QF_POSTGRES_SINK_ENABLED=1``.
    """
    return postgres_sink_enabled()


def should_read_from_postgres() -> bool:
    """Return True if settlement records should be read from Postgres.

    True when ``QF_POSTGRES_READS_ENABLED=1`` AND
    ``QF_LEGACY_FILE_READ_FALLBACK=0``.
    """
    return postgres_reads_enabled() and not legacy_file_read_fallback()


def should_read_compare() -> bool:
    """Return True if read-compare mode is active.

    True when ``QF_POSTGRES_READ_COMPARE_ENABLED=1``. This is independent
    of ``QF_POSTGRES_READS_ENABLED`` — read-compare always returns the
    legacy record to the caller; it only *compares* against Postgres.
    """
    return postgres_read_compare_enabled()


def should_write_to_jsonl() -> bool:
    """Return True if settlement records should still be written to JSONL.

    True when:
      - Postgres sink is off (JSONL is the only writer), OR
      - Postgres sink is on AND dual-write is enabled.
    """
    if not postgres_sink_enabled():
        return True
    return dual_write_settlements()
