"""
quant_foundry.callback_dlq — Callback dead-letter queue with backoff (Phase 6 / T-6.2).

The DLQ is the durable quarantine store for rejected callbacks. When the
callback gateway rejects an inbound callback (bad signature, tampered
payload, duplicate, missing fields, stale manifest, etc.) the rejection is
recorded here so an operator can audit it and, for retryable failures,
schedule a retry with exponential backoff.

Design (mirrors outbox.py / inbox.py / job_ledger.py for consistency):

- Append-only JSONL under ``<base_dir>/callback_dlq.jsonl``. Each line is
  the full ``DLQRecord`` at write time. On reload the last line per
  ``dlq_id`` wins (last-writer-wins by file order).
- Pydantic v2 ``BaseModel`` with ``frozen=True`` and ``extra="forbid"``
  for audit integrity (matches OutboxRecord / InboxRecord /
  JobLedgerRecord).
- ``StrEnum`` for the rejection reason enum (matches JobStatus /
  CallbackStatus / JobLedgerState).
- ``fsync`` after every write (best-effort on platforms without it).
- Idempotency: the idempotency key is ``f"{job_id}:{manifest_hash}"``.
  ``is_duplicate(job_id, manifest_hash)`` checks whether a DLQ entry
  already exists for that key so a duplicate callback is not
  double-processed (no double-promote, no double-verify).

Security invariant (enforced):
- ``SIGNATURE_FAILED`` and ``PAYLOAD_TAMPER`` rejections are NEVER
  retryable. They are stored for audit only. The payload is never
  re-processed. ``enqueue()`` forces ``is_retryable=False`` for these
  reasons regardless of what the caller passes.

Retry policy:
- Retryable failures (e.g. ``STALE_MANIFEST``) schedule a retry with
  exponential backoff: ``base * 2**retry_count`` capped at 300 seconds.
- ``record_retry(dlq_id)`` increments ``retry_count`` and recomputes
  ``next_retry_at_ns``.
- ``mark_terminal(dlq_id)`` marks the entry as non-retryable (max retries
  reached or a security failure). Terminal entries are kept for audit.
- ``get_retryable_due()`` returns entries whose ``next_retry_at_ns`` is in
  the past and whose ``retry_count < max_retries``.
"""

from __future__ import annotations

import json
import pathlib
import time
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class DLQRejectionReason(StrEnum):
    """Reasons a callback is rejected to the DLQ.

    Security reasons (never retryable — audit only):
      - SIGNATURE_FAILED: HMAC signature verification failed.
      - PAYLOAD_TAMPER: payload hash mismatch (tamper/replay detected).
      - DUPLICATE_CALLBACK: duplicate callback for an already-seen key.
      - MISSING_REQUIRED_FIELDS: callback missing required fields.
      - JOB_ID_MISMATCH: callback job_id does not match a known job.
      - ARTIFACT_VERIFY_FAILED: artifact verification failed.
      - DOMAIN_EFFECT_FAILED: applying the domain effect failed.

    Retryable reasons (transient — may succeed on retry):
      - STALE_MANIFEST: manifest is stale; a refresh may fix it.
    """

    SIGNATURE_FAILED = "signature_failed"
    MISSING_REQUIRED_FIELDS = "missing_required_fields"
    ARTIFACT_VERIFY_FAILED = "artifact_verify_failed"
    DUPLICATE_CALLBACK = "duplicate_callback"
    STALE_MANIFEST = "stale_manifest"
    PAYLOAD_TAMPER = "payload_tamper"
    INVALID_SCHEMA = "invalid_schema"
    JOB_ID_MISMATCH = "job_id_mismatch"
    DOMAIN_EFFECT_FAILED = "domain_effect_failed"


# Rejection reasons that are NEVER retryable (security / audit only).
# ``enqueue()`` forces ``is_retryable=False`` for these regardless of the
# caller's request. This is the security invariant: a bad signature or a
# tampered payload is never re-processed.
_NON_RETRYABLE_REASONS: frozenset[DLQRejectionReason] = frozenset(
    {
        DLQRejectionReason.SIGNATURE_FAILED,
        DLQRejectionReason.PAYLOAD_TAMPER,
        DLQRejectionReason.DUPLICATE_CALLBACK,
        DLQRejectionReason.MISSING_REQUIRED_FIELDS,
        DLQRejectionReason.JOB_ID_MISMATCH,
        DLQRejectionReason.ARTIFACT_VERIFY_FAILED,
        DLQRejectionReason.DOMAIN_EFFECT_FAILED,
        DLQRejectionReason.INVALID_SCHEMA,
    }
)

# Cap for exponential backoff (seconds). ``base * 2**retry_count`` is
# capped at this value so a long retry chain does not schedule a retry
# hours in the future.
_BACKOFF_CAP_SECONDS: float = 300.0


class DLQRecord(BaseModel):
    """Durable record of one rejected callback in the DLQ.

    Frozen + ``extra="forbid"`` for audit integrity (matches
    ``OutboxRecord`` / ``InboxRecord`` / ``JobLedgerRecord``). One row per
    rejected callback; the row is rewritten (appended) on every retry
    attempt so the JSONL file is a complete replay of the entry's history.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    dlq_id: str
    callback_id: str | None = None
    job_id: str
    manifest_hash: str
    idempotency_key: str
    rejection_reason: DLQRejectionReason
    rejection_detail: str
    payload_ref: str | None = None
    retry_count: int = 0
    max_retries: int = 3
    next_retry_at_ns: int | None = None
    backoff_base_seconds: float = 1.0
    is_retryable: bool
    created_at_ns: int
    updated_at_ns: int
    # Append-only history. Each entry:
    # {"event", "ts_ns", ...optional}
    history: tuple[dict[str, Any], ...] = Field(default_factory=tuple)


class CallbackDLQ:
    """Append-only JSONL-backed callback dead-letter queue.

    One process, one writer per file. File path:
    ``<base_dir>/callback_dlq.jsonl``. Each line is the full ``DLQRecord``
    JSON at write time. Reload replays all lines and keeps the last record
    per ``dlq_id`` (last-writer-wins by file order). A secondary index on
    ``idempotency_key`` supports duplicate detection.
    """

    FILENAME = "callback_dlq.jsonl"

    def __init__(self, base_dir: pathlib.Path | str) -> None:
        self.base_dir = pathlib.Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.base_dir / self.FILENAME
        # In-memory index: dlq_id -> latest DLQRecord
        self._records: dict[str, DLQRecord] = {}
        # Secondary index: idempotency_key -> latest DLQRecord (last wins)
        self._by_idempotency: dict[str, DLQRecord] = {}
        self._reload()

    # --- durability ---

    def _reload(self) -> None:
        """Replay JSONL from disk. Last line per dlq_id wins."""
        self._records = {}
        self._by_idempotency = {}
        if not self.path.is_file():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                rec = DLQRecord.model_validate(data)
                self._records[rec.dlq_id] = rec
                self._by_idempotency[rec.idempotency_key] = rec

    def _append(self, rec: DLQRecord) -> None:
        """Append one record line and fsync for durability."""
        line = rec.model_dump_json()
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            try:
                import os

                os.fsync(fh.fileno())
            except (OSError, AttributeError):
                # fsync best-effort on platforms that don't support it.
                pass

    def _store(self, rec: DLQRecord) -> DLQRecord:
        """Persist a record and update in-memory indexes."""
        self._append(rec)
        self._records[rec.dlq_id] = rec
        self._by_idempotency[rec.idempotency_key] = rec
        return rec

    # --- public API ---

    def enqueue(
        self,
        job_id: str,
        manifest_hash: str,
        rejection_reason: DLQRejectionReason,
        rejection_detail: str,
        *,
        callback_id: str | None = None,
        payload_ref: str | None = None,
        is_retryable: bool = False,
        max_retries: int = 3,
        backoff_base_seconds: float = 1.0,
    ) -> DLQRecord:
        """Enqueue a rejected callback to the DLQ.

        Computes the idempotency key ``f"{job_id}:{manifest_hash}"``. If
        an entry already exists for that key, the existing entry is
        returned unchanged (idempotent enqueue — a duplicate rejection
        does not create a second DLQ row).

        Security invariant: if ``rejection_reason`` is in
        ``_NON_RETRYABLE_REASONS`` (SIGNATURE_FAILED, PAYLOAD_TAMPER,
        etc.), ``is_retryable`` is forced to ``False`` regardless of the
        caller's request. Security failures are audit-only and are never
        re-processed.

        For retryable failures, ``next_retry_at_ns`` is set to
        ``now + compute_backoff(0, base)`` so the first retry is due after
        the initial backoff window.

        Args:
            job_id: the outbox job id the callback was for.
            manifest_hash: the manifest hash (or outbox idempotency key)
                used to build the DLQ idempotency key.
            rejection_reason: the reason the callback was rejected.
            rejection_detail: human-readable detail for the rejection.
            callback_id: optional reference to the inbox record id, if any.
            payload_ref: optional reference to the stored payload, if any.
            is_retryable: whether the failure is retryable. Forced False
                for security reasons.
            max_retries: max retry attempts before marking terminal.
            backoff_base_seconds: base for exponential backoff.

        Raises:
            ValueError: if ``job_id`` or ``manifest_hash`` is empty.
        """
        if not job_id or not isinstance(job_id, str):
            raise ValueError("job_id must be non-empty str")
        if not manifest_hash or not isinstance(manifest_hash, str):
            raise ValueError("manifest_hash must be non-empty str")

        idempotency_key = f"{job_id}:{manifest_hash}"

        # Idempotent enqueue: if an entry already exists for this key,
        # return it unchanged. A duplicate rejection does not create a
        # second DLQ row (and never double-processes).
        existing = self._by_idempotency.get(idempotency_key)
        if existing is not None:
            return existing

        # Security invariant: force non-retryable for security reasons.
        effective_retryable = is_retryable and rejection_reason not in _NON_RETRYABLE_REASONS

        now = time.time_ns()
        next_retry_at_ns: int | None = None
        if effective_retryable:
            delay_s = self.compute_backoff(0, backoff_base_seconds)
            next_retry_at_ns = now + int(delay_s * 1_000_000_000)

        dlq_id = f"dlq:{job_id}:{now}"
        history_entry: dict[str, Any] = {
            "event": "enqueued",
            "ts_ns": now,
            "rejection_reason": rejection_reason.value,
            "is_retryable": effective_retryable,
        }
        rec = DLQRecord(
            dlq_id=dlq_id,
            callback_id=callback_id,
            job_id=job_id,
            manifest_hash=manifest_hash,
            idempotency_key=idempotency_key,
            rejection_reason=rejection_reason,
            rejection_detail=rejection_detail,
            payload_ref=payload_ref,
            retry_count=0,
            max_retries=max_retries,
            next_retry_at_ns=next_retry_at_ns,
            backoff_base_seconds=backoff_base_seconds,
            is_retryable=effective_retryable,
            created_at_ns=now,
            updated_at_ns=now,
            history=(history_entry,),
        )
        return self._store(rec)

    def record_retry(self, dlq_id: str) -> DLQRecord:
        """Record a retry attempt for a DLQ entry.

        Increments ``retry_count``, recomputes ``next_retry_at_ns`` using
        exponential backoff, and appends a history entry. If the entry
        reaches ``max_retries``, it is marked terminal (``is_retryable``
        set to ``False``, ``next_retry_at_ns`` cleared).

        Raises:
            KeyError: if ``dlq_id`` is unknown.
            ValueError: if the entry is not retryable.
        """
        existing = self._records.get(dlq_id)
        if existing is None:
            raise KeyError(f"unknown dlq_id: {dlq_id}")
        if not existing.is_retryable:
            raise ValueError(
                f"dlq entry {dlq_id} is not retryable (reason={existing.rejection_reason.value})"
            )

        now = time.time_ns()
        new_retry_count = existing.retry_count + 1
        reached_max = new_retry_count >= existing.max_retries

        if reached_max:
            # Mark terminal: no further retries.
            history_entry: dict[str, Any] = {
                "event": "retry_exhausted",
                "ts_ns": now,
                "retry_count": new_retry_count,
                "max_retries": existing.max_retries,
            }
            updated = existing.model_copy(
                update={
                    "retry_count": new_retry_count,
                    "is_retryable": False,
                    "next_retry_at_ns": None,
                    "updated_at_ns": now,
                    "history": (*existing.history, history_entry),
                }
            )
        else:
            delay_s = self.compute_backoff(new_retry_count, existing.backoff_base_seconds)
            next_retry_at_ns = now + int(delay_s * 1_000_000_000)
            history_entry = {
                "event": "retry_scheduled",
                "ts_ns": now,
                "retry_count": new_retry_count,
                "next_retry_at_ns": next_retry_at_ns,
                "backoff_seconds": delay_s,
            }
            updated = existing.model_copy(
                update={
                    "retry_count": new_retry_count,
                    "next_retry_at_ns": next_retry_at_ns,
                    "updated_at_ns": now,
                    "history": (*existing.history, history_entry),
                }
            )
        return self._store(updated)

    def mark_terminal(self, dlq_id: str) -> DLQRecord:
        """Mark a DLQ entry as terminal (non-retryable).

        Clears ``next_retry_at_ns`` and sets ``is_retryable=False``. Used
        when a security failure is detected or an operator manually
        quarantines an entry. The entry is kept for audit.

        Raises:
            KeyError: if ``dlq_id`` is unknown.
        """
        existing = self._records.get(dlq_id)
        if existing is None:
            raise KeyError(f"unknown dlq_id: {dlq_id}")

        now = time.time_ns()
        history_entry: dict[str, Any] = {
            "event": "marked_terminal",
            "ts_ns": now,
        }
        updated = existing.model_copy(
            update={
                "is_retryable": False,
                "next_retry_at_ns": None,
                "updated_at_ns": now,
                "history": (*existing.history, history_entry),
            }
        )
        return self._store(updated)

    def get(self, dlq_id: str) -> DLQRecord | None:
        """Return the latest record for ``dlq_id``, or None."""
        return self._records.get(dlq_id)

    def get_by_idempotency(self, idempotency_key: str) -> DLQRecord | None:
        """Return the latest record for ``idempotency_key``, or None."""
        return self._by_idempotency.get(idempotency_key)

    def is_duplicate(self, job_id: str, manifest_hash: str) -> bool:
        """Check whether a DLQ entry already exists for this key.

        The idempotency key is ``f"{job_id}:{manifest_hash}"``. Returns
        ``True`` if an entry exists (the callback is a duplicate rejection
        and should not be double-processed).
        """
        idempotency_key = f"{job_id}:{manifest_hash}"
        return idempotency_key in self._by_idempotency

    def list(
        self,
        *,
        reason: DLQRejectionReason | None = None,
        retryable: bool | None = None,
        limit: int = 100,
    ) -> list[DLQRecord]:
        """List DLQ entries, optionally filtered by reason / retryability.

        Order is insertion order of the in-memory dict (which reflects
        last-seen order on reload). ``limit`` caps the number returned;
        ``limit <= 0`` returns all.
        """
        records = list(self._records.values())
        if reason is not None:
            records = [r for r in records if r.rejection_reason == reason]
        if retryable is not None:
            records = [r for r in records if r.is_retryable == retryable]
        if limit and limit > 0:
            records = records[:limit]
        return records

    def get_retryable_due(self) -> list[DLQRecord]:
        """Return retryable entries whose next retry is due now.

        An entry is due if ``next_retry_at_ns`` is set and
        ``<= time.time_ns()``, and ``retry_count < max_retries``, and
        ``is_retryable`` is True. Entries with no scheduled retry (e.g.
        terminal or not-yet-retryable) are excluded.
        """
        now = time.time_ns()
        due: list[DLQRecord] = []
        for rec in self._records.values():
            if not rec.is_retryable:
                continue
            if rec.retry_count >= rec.max_retries:
                continue
            if rec.next_retry_at_ns is None:
                continue
            if rec.next_retry_at_ns <= now:
                due.append(rec)
        return due

    @staticmethod
    def compute_backoff(retry_count: int, base_seconds: float) -> float:
        """Compute exponential backoff delay in seconds.

        ``base_seconds * 2**retry_count``, capped at 300 seconds. A
        non-positive ``base_seconds`` returns 0 (no delay). A negative
        ``retry_count`` is treated as 0.

        Args:
            retry_count: the number of retries already attempted (0 for
                the first retry).
            base_seconds: the base delay in seconds.

        Returns:
            The delay in seconds before the next retry attempt.
        """
        if base_seconds <= 0:
            return 0.0
        n = max(0, retry_count)
        delay = base_seconds * (2**n)
        return min(delay, _BACKOFF_CAP_SECONDS)
