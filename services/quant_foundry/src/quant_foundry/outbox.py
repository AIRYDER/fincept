"""
quant_foundry.outbox — Durable local job outbox (TASK-0304).

Stores outbound job records *before* dispatch so a process crash never
loses a job. MVP uses append-only JSONL under a configurable base_dir
(default `reports/quant-foundry`). Migration path to Postgres/Timescale
is preserved by keeping the record schema explicit and stable.

Invariants (enforced):
- Append-only JSONL: every enqueue / status change appends one line with
  the full current record (history included). On restart the last line
  per job_id wins — simple, durable, replayable.
- Idempotent enqueue: same job_id + same request_payload_hash returns the
  existing record (no duplicate effects, no error).
- Security: same job_id + DIFFERENT request_payload_hash is rejected as a
  security event (ValueError). This catches tampered / replayed re-enqueue
  attempts that try to swap a job's payload under a known id.
- History is append-only and ordered; receipts include the full history.
- Timestamps are monotonic nanoseconds (time.time_ns) for ordering.

This is local durability only — not production-grade (no fsync batching,
no WAL, no concurrency control). It is explicitly an MVP per TASK-0304.
"""

from __future__ import annotations

import json
import pathlib
import time
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from quant_foundry.ids import hash_payload


class JobStatus(StrEnum):
    """Lifecycle states for an outbox job.

    Transitions (typical):
      queued -> dispatching -> dispatched -> running
              -> callback_received -> validating -> completed | failed
    Failed is terminal-reachable from any non-terminal state.
    """

    QUEUED = "queued"
    DISPATCHING = "dispatching"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    CALLBACK_RECEIVED = "callback_received"
    VALIDATING = "validating"
    COMPLETED = "completed"
    FAILED = "failed"


# Statuses that may legally follow a given status. Kept permissive for MVP
# (dispatcher / callback processor drive transitions); the outbox itself
# only records what it is told and never silently drops a transition.
_ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset(JobStatus),
    JobStatus.DISPATCHING: frozenset(JobStatus),
    JobStatus.DISPATCHED: frozenset(JobStatus),
    JobStatus.RUNNING: frozenset(JobStatus),
    JobStatus.CALLBACK_RECEIVED: frozenset(JobStatus),
    JobStatus.VALIDATING: frozenset(JobStatus),
    JobStatus.COMPLETED: frozenset(),  # terminal
    JobStatus.FAILED: frozenset(),  # terminal
}


class OutboxRecord(BaseModel):
    """Durable record of one outbound job. Frozen + extra='forbid' for safety."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    job_id: str
    job_type: str
    idempotency_key: str
    status: JobStatus
    request_payload_hash: str
    request_payload_ref: str | None = None
    created_at_ns: int
    updated_at_ns: int
    attempt_count: int = 0
    next_retry_at_ns: int | None = None
    runpod_endpoint_id: str | None = None
    runpod_job_id: str | None = None
    timeout_seconds: int | None = None
    priority: int = 0
    budget_cents: int | None = None
    error_code: str | None = None
    error_summary: str | None = None
    # Append-only transition history. Each entry: {"status", "ts_ns", ...optional}
    history: list[dict[str, Any]] = Field(default_factory=list)


def _serialize_payload(payload: Any) -> bytes:
    """Canonical bytes for hashing. Matches test expectation:
    dict/list -> json.dumps(sort_keys=True).encode(); bytes -> as-is;
    str -> utf-8; None -> b"".
    """
    if payload is None:
        return b""
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload)
    if isinstance(payload, str):
        return payload.encode("utf-8")
    if isinstance(payload, (dict, list, tuple)):
        return json.dumps(payload, sort_keys=True).encode("utf-8")
    # Numbers / bools / other: JSON-encode for stable bytes.
    return json.dumps(payload, sort_keys=True).encode("utf-8")


class JobOutbox:
    """Append-only JSONL-backed outbox. One process, one writer per file.

    The file path is `<base_dir>/outbox.jsonl`. Each line is the full
    OutboxRecord JSON at the moment of write. Reload replays all lines and
    keeps the last record per job_id (last-writer-wins by file order).
    """

    FILENAME = "outbox.jsonl"

    def __init__(self, base_dir: pathlib.Path | str) -> None:
        self.base_dir = pathlib.Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.base_dir / self.FILENAME
        # In-memory index: job_id -> latest OutboxRecord
        self._records: dict[str, OutboxRecord] = {}
        self._reload()

    # --- durability ---

    def _reload(self) -> None:
        """Replay JSONL from disk. Last line per job_id wins."""
        self._records = {}
        if not self.path.is_file():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                rec = OutboxRecord.model_validate(data)
                self._records[rec.job_id] = rec

    def _append(self, rec: OutboxRecord) -> None:
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

    # --- public API ---

    def enqueue(
        self,
        *,
        job_id: str,
        job_type: str,
        idempotency_key: str,
        request_payload: Any,
        priority: int = 0,
        budget_cents: int | None = None,
        timeout_seconds: int | None = None,
        request_payload_ref: str | None = None,
    ) -> OutboxRecord:
        """Enqueue a new job or return the existing one (idempotent).

        Security: if job_id already exists with a DIFFERENT payload hash,
        raise ValueError (security event). Same job_id + same hash is
        idempotent and returns the existing record unchanged.
        """
        if not job_id or not isinstance(job_id, str):
            raise ValueError("job_id must be non-empty str")
        payload_bytes = _serialize_payload(request_payload)
        payload_hash = hash_payload(payload_bytes)

        existing = self._records.get(job_id)
        if existing is not None:
            if existing.request_payload_hash != payload_hash:
                # Security event: someone tried to re-enqueue the same
                # job_id with a tampered/different payload. Fail closed.
                raise ValueError(
                    "payload hash mismatch for existing job_id "
                    f"{job_id}: possible tamper / replay (security event)"
                )
            # Idempotent re-enqueue: return existing record, no new write.
            return existing

        now = time.time_ns()
        history_entry: dict[str, Any] = {"status": JobStatus.QUEUED.value, "ts_ns": now}
        rec = OutboxRecord(
            job_id=job_id,
            job_type=job_type,
            idempotency_key=idempotency_key,
            status=JobStatus.QUEUED,
            request_payload_hash=payload_hash,
            request_payload_ref=request_payload_ref,
            created_at_ns=now,
            updated_at_ns=now,
            priority=priority,
            budget_cents=budget_cents,
            timeout_seconds=timeout_seconds,
            history=[history_entry],
        )
        self._append(rec)
        self._records[job_id] = rec
        return rec

    def update_status(
        self,
        job_id: str,
        status: JobStatus,
        *,
        runpod_endpoint_id: str | None = None,
        runpod_job_id: str | None = None,
        attempt_count: int | None = None,
        next_retry_at_ns: int | None = None,
        error_code: str | None = None,
        error_summary: str | None = None,
        note: str | None = None,
    ) -> OutboxRecord:
        """Transition a job to a new status, appending to history.

        Raises KeyError if job_id unknown. Permissive on transitions for
        MVP (dispatcher drives); terminal states (completed/failed) are
        sticky — re-updating them is allowed but only appends history.
        """
        existing = self._records.get(job_id)
        if existing is None:
            raise KeyError(f"unknown job_id: {job_id}")

        now = time.time_ns()
        history_entry: dict[str, Any] = {"status": status.value, "ts_ns": now}
        if runpod_endpoint_id is not None:
            history_entry["runpod_endpoint_id"] = runpod_endpoint_id
        if runpod_job_id is not None:
            history_entry["runpod_job_id"] = runpod_job_id
        if error_code is not None:
            history_entry["error_code"] = error_code
        if error_summary is not None:
            history_entry["error_summary"] = error_summary
        if note is not None:
            history_entry["note"] = note

        new_history = [*existing.history, history_entry]
        # Pydantic frozen: build a new model.
        updated = existing.model_copy(
            update={
                "status": status,
                "updated_at_ns": now,
                "runpod_endpoint_id": runpod_endpoint_id or existing.runpod_endpoint_id,
                "runpod_job_id": runpod_job_id or existing.runpod_job_id,
                "attempt_count": attempt_count
                if attempt_count is not None
                else existing.attempt_count,
                "next_retry_at_ns": next_retry_at_ns,
                "error_code": error_code,
                "error_summary": error_summary,
                "history": new_history,
            }
        )
        self._append(updated)
        self._records[job_id] = updated
        return updated

    def get(self, job_id: str) -> OutboxRecord | None:
        """Return the latest record for job_id, or None."""
        return self._records.get(job_id)

    def list(self, *, status: JobStatus | None = None) -> list[OutboxRecord]:
        """List records, optionally filtered by status. Order is insertion
        order of the in-memory dict (which reflects last-seen order on
        reload)."""
        records = list(self._records.values())
        if status is not None:
            records = [r for r in records if r.status == status]
        return records

    def receipt(self, job_id: str) -> dict[str, Any] | None:
        """Return a receipt dict (record + full history) for a job."""
        rec = self._records.get(job_id)
        if rec is None:
            return None
        return rec.model_dump()
