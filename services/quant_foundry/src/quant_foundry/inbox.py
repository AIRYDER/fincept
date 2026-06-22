"""
quant_foundry.inbox — Durable local callback inbox (TASK-0304).

Stores inbound callback records *before* any domain processing so a crash
during processing can be safely retried without re-calling the worker.

Invariants (enforced):
- Append-only JSONL under `<base_dir>/inbox.jsonl`. Reload keeps the last
  record per callback_id; a job_id index is also maintained for lookup.
- Idempotent receive: same job_id + same payload_hash is a duplicate —
  status becomes DUPLICATE, no error, no duplicate side effects.
- Security: same job_id + DIFFERENT payload_hash is rejected as a security
  event (ValueError). Catches tampered / replayed callbacks that try to
  swap results under a known job.
- signature_valid is recorded but NOT enforced here (the signature layer
  is signatures.py; the inbox just records the verdict for audit).
- processed_at_ns + status track downstream processing.

MVP only — local JSONL, no concurrency control. See TASK-0304 risk note.
"""

from __future__ import annotations

import json
import pathlib
import time
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from quant_foundry.ids import hash_payload


class CallbackStatus(StrEnum):
    """Lifecycle states for an inbound callback."""

    RECEIVED = "received"
    DUPLICATE = "duplicate"
    PROCESSED = "processed"
    REJECTED = "rejected"
    FAILED = "failed"


class InboxRecord(BaseModel):
    """Durable record of one inbound callback. Frozen + extra='forbid'."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    callback_id: str
    job_id: str
    idempotency_key: str
    signature_valid: bool
    payload_hash: str
    payload_ref: str | None = None
    worker_id: str | None = None
    received_at_ns: int
    processed_at_ns: int | None = None
    status: CallbackStatus
    error_code: str | None = None
    error_summary: str | None = None
    # Append-only history. Each entry: {"status", "ts_ns", ...optional}
    history: list[dict[str, Any]] = Field(default_factory=list)


def _serialize_payload(payload: Any) -> bytes:
    """Canonical bytes for hashing (mirrors outbox._serialize_payload)."""
    if payload is None:
        return b""
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload)
    if isinstance(payload, str):
        return payload.encode("utf-8")
    if isinstance(payload, (dict, list, tuple)):
        return json.dumps(payload, sort_keys=True).encode("utf-8")
    return json.dumps(payload, sort_keys=True).encode("utf-8")


class CallbackInbox:
    """Append-only JSONL-backed inbox. One process, one writer per file.

    File path: `<base_dir>/inbox.jsonl`. Each line is the full InboxRecord
    JSON at write time. Reload replays all lines; last record per
    callback_id wins, and a job_id -> record index is rebuilt for lookup.
    """

    FILENAME = "inbox.jsonl"

    def __init__(self, base_dir: pathlib.Path | str) -> None:
        self.base_dir = pathlib.Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.base_dir / self.FILENAME
        self._by_callback_id: dict[str, InboxRecord] = {}
        self._by_job_id: dict[str, InboxRecord] = {}
        self._reload()

    # --- durability ---

    def _reload(self) -> None:
        self._by_callback_id = {}
        self._by_job_id = {}
        if not self.path.is_file():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                rec = InboxRecord.model_validate(data)
                self._by_callback_id[rec.callback_id] = rec
                # job_id index: last record wins (most recent callback)
                self._by_job_id[rec.job_id] = rec

    def _append(self, rec: InboxRecord) -> None:
        line = rec.model_dump_json()
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            try:
                import os

                os.fsync(fh.fileno())
            except (OSError, AttributeError):
                pass

    # --- public API ---

    def receive(
        self,
        *,
        job_id: str,
        idempotency_key: str,
        signature_valid: bool,
        payload: Any,
        worker_id: str | None = None,
        payload_ref: str | None = None,
    ) -> InboxRecord:
        """Receive a callback. Idempotent on (job_id, payload_hash).

        Security: same job_id + DIFFERENT payload_hash -> ValueError.
        """
        if not job_id or not isinstance(job_id, str):
            raise ValueError("job_id must be non-empty str")
        payload_bytes = _serialize_payload(payload)
        payload_hash = hash_payload(payload_bytes)

        existing = self._by_job_id.get(job_id)
        if existing is not None:
            if existing.payload_hash != payload_hash:
                # Security event: tampered / replayed callback with a
                # different payload under a known job_id. Fail closed.
                raise ValueError(
                    "payload hash mismatch for existing job_id "
                    f"{job_id}: different payload (security event)"
                )
            # Idempotent duplicate: record a DUPLICATE entry referencing
            # the same payload hash. No duplicate domain effects.
            now = time.time_ns()
            dup_history = [
                *existing.history,
                {"status": CallbackStatus.DUPLICATE.value, "ts_ns": now, "duplicate": True},
            ]
            dup = existing.model_copy(
                update={
                    "status": CallbackStatus.DUPLICATE,
                    "processed_at_ns": existing.processed_at_ns,
                    "history": dup_history,
                }
            )
            self._append(dup)
            self._by_callback_id[dup.callback_id] = dup
            self._by_job_id[job_id] = dup
            return dup

        now = time.time_ns()
        callback_id = f"cb:{job_id}:{now}"
        rec = InboxRecord(
            callback_id=callback_id,
            job_id=job_id,
            idempotency_key=idempotency_key,
            signature_valid=signature_valid,
            payload_hash=payload_hash,
            payload_ref=payload_ref,
            worker_id=worker_id,
            received_at_ns=now,
            status=CallbackStatus.RECEIVED,
            history=[{"status": CallbackStatus.RECEIVED.value, "ts_ns": now}],
        )
        self._append(rec)
        self._by_callback_id[callback_id] = rec
        self._by_job_id[job_id] = rec
        return rec

    def get_by_job_id(self, job_id: str) -> InboxRecord | None:
        """Return the latest record for job_id, or None."""
        return self._by_job_id.get(job_id)

    def get(self, callback_id: str) -> InboxRecord | None:
        """Return the record for callback_id, or None."""
        return self._by_callback_id.get(callback_id)

    def mark_processed(
        self,
        job_id: str,
        *,
        status: CallbackStatus = CallbackStatus.PROCESSED,
        note: str | None = None,
        error_code: str | None = None,
        error_summary: str | None = None,
    ) -> InboxRecord:
        """Mark the latest callback for a job as processed (or failed/rejected).

        Sets processed_at_ns and appends a history entry.
        """
        existing = self._by_job_id.get(job_id)
        if existing is None:
            raise KeyError(f"unknown job_id: {job_id}")
        now = time.time_ns()
        history_entry: dict[str, Any] = {"status": status.value, "ts_ns": now}
        if note is not None:
            history_entry["note"] = note
        if error_code is not None:
            history_entry["error_code"] = error_code
        if error_summary is not None:
            history_entry["error_summary"] = error_summary
        updated = existing.model_copy(
            update={
                "status": status,
                "processed_at_ns": now,
                "error_code": error_code,
                "error_summary": error_summary,
                "history": [*existing.history, history_entry],
            }
        )
        self._append(updated)
        self._by_callback_id[updated.callback_id] = updated
        self._by_job_id[job_id] = updated
        return updated

    def list(self, *, status: CallbackStatus | None = None) -> list[InboxRecord]:
        """List records (by callback_id), optionally filtered by status."""
        records = list(self._by_callback_id.values())
        if status is not None:
            records = [r for r in records if r.status == status]
        return records
