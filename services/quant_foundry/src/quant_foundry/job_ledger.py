"""
quant_foundry.job_ledger — Append-only training job ledger (Phase 6 / T-6.1).

The ledger is the durable audit trail for one training job's entire
lifecycle: outbox enqueue -> RunPod dispatch -> callback -> artifact
verification -> terminal state. It links together the outbox job id,
the RunPod job id, the dataset id, the verified artifact id, every
callback received, every failure, every retry, and the cumulative cost.

Design (mirrors outbox.py / inbox.py for consistency):

- Append-only JSONL under ``<base_dir>/job_ledger.jsonl``. Each line is
  the full ``JobLedgerRecord`` at write time. On restart the last line
  per ``ledger_id`` wins (last-writer-wins by file order).
- Pydantic v2 ``BaseModel`` with ``frozen=True`` and ``extra="forbid"``
  for audit integrity (matches OutboxRecord / InboxRecord).
- ``StrEnum`` for the state enum (matches JobStatus / CallbackStatus).
- ``fsync`` after every write (best-effort on platforms without it).
- The ledger is READ-ONLY with respect to the outbox: it observes state
  transitions but never drives them. The outbox remains the source of
  truth for job state; the ledger is the audit trail that lets an
  operator trace one job end-to-end without reading logs.

State machine (``JobLedgerState``)::

    queued -> dispatched -> runpod_running -> callback_received
                                                     |
                                                     v
                                          artifact_verified  (terminal-good)
                                          rejected           (terminal-bad)
                                          failed             (terminal-bad)
                                          expired            (terminal-bad)

``queued`` is the initial state. ``failed`` / ``rejected`` / ``expired`` /
``artifact_verified`` are terminal. Transitions are permissive (the
dispatcher / callback processor drive them); the ledger records what it
is told and never silently drops a transition.
"""

from __future__ import annotations

import json
import pathlib
import time
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class JobLedgerState(StrEnum):
    """Lifecycle states for a ledger row.

    Transitions (typical):
      queued -> dispatched -> runpod_running -> callback_received
              -> artifact_verified | rejected | failed | expired
    ``failed`` / ``rejected`` / ``expired`` / ``artifact_verified`` are
    terminal.
    """

    QUEUED = "queued"
    DISPATCHED = "dispatched"
    RUNPOD_RUNNING = "runpod_running"
    CALLBACK_RECEIVED = "callback_received"
    ARTIFACT_VERIFIED = "artifact_verified"
    REJECTED = "rejected"
    FAILED = "failed"
    EXPIRED = "expired"


# Terminal states (no further transitions expected). Kept as a set for
# quick membership checks; the ledger still accepts updates to terminal
# rows (e.g. a late cost record) but they only append history, they do
# not change the state.
_TERMINAL_STATES: frozenset[JobLedgerState] = frozenset(
    {
        JobLedgerState.ARTIFACT_VERIFIED,
        JobLedgerState.REJECTED,
        JobLedgerState.FAILED,
        JobLedgerState.EXPIRED,
    }
)


class JobLedgerRecord(BaseModel):
    """Durable record of one training job's lifecycle.

    Frozen + ``extra="forbid"`` for audit integrity (matches
    ``OutboxRecord`` / ``InboxRecord``). One row per job; the row is
    rewritten (appended) on every state transition so the JSONL file is
    a complete replay of the job's history.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    ledger_id: str
    outbox_id: str
    runpod_job_id: str | None = None
    dataset_id: str | None = None
    artifact_id: str | None = None
    callbacks: tuple[str, ...] = Field(default_factory=tuple)
    failures: tuple[dict[str, Any], ...] = Field(default_factory=tuple)
    retries: int = 0
    cost_cents: int = 0
    duration_seconds: float = 0.0
    state: JobLedgerState = JobLedgerState.QUEUED
    created_at_ns: int
    updated_at_ns: int
    # Append-only transition history. Each entry:
    # {"state", "ts_ns", ...optional fields}
    history: tuple[dict[str, Any], ...] = Field(default_factory=tuple)


class TrainingJobLedger:
    """Append-only JSONL-backed training job ledger.

    One process, one writer per file. File path:
    ``<base_dir>/job_ledger.jsonl``. Each line is the full
    ``JobLedgerRecord`` JSON at write time. Reload replays all lines and
    keeps the last record per ``ledger_id`` (last-writer-wins by file
    order).

    The ledger is read-only with respect to the outbox: it observes
    state transitions but never drives them. Callers (the dispatcher,
    the callback processor, the artifact verifier) tell the ledger what
    happened; the ledger records it durably.
    """

    FILENAME = "job_ledger.jsonl"

    def __init__(self, base_dir: pathlib.Path | str) -> None:
        self.base_dir = pathlib.Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.base_dir / self.FILENAME
        # In-memory index: ledger_id -> latest JobLedgerRecord
        self._records: dict[str, JobLedgerRecord] = {}
        # Secondary index: outbox_id -> latest JobLedgerRecord (last wins)
        self._by_outbox_id: dict[str, JobLedgerRecord] = {}
        self._reload()

    # --- durability ---

    def _reload(self) -> None:
        """Replay JSONL from disk. Last line per ledger_id wins."""
        self._records = {}
        self._by_outbox_id = {}
        if not self.path.is_file():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                rec = JobLedgerRecord.model_validate(data)
                self._records[rec.ledger_id] = rec
                self._by_outbox_id[rec.outbox_id] = rec

    def _append(self, rec: JobLedgerRecord) -> None:
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

    def _store(self, rec: JobLedgerRecord) -> JobLedgerRecord:
        """Persist a record and update in-memory indexes."""
        self._append(rec)
        self._records[rec.ledger_id] = rec
        self._by_outbox_id[rec.outbox_id] = rec
        return rec

    # --- public API ---

    def create_row(
        self,
        outbox_id: str,
        *,
        dataset_id: str | None = None,
        ledger_id: str | None = None,
    ) -> JobLedgerRecord:
        """Create a new ledger row in the QUEUED state.

        Args:
            outbox_id: reference to the outbox job_id (source of truth).
            dataset_id: optional dataset id known at enqueue time.
            ledger_id: optional explicit ledger id. Defaults to the
                outbox_id (one ledger row per outbox job).

        Raises:
            ValueError: if ``outbox_id`` is empty, or if a row already
                exists for the resolved ``ledger_id`` (idempotent
                re-create is rejected to keep the audit trail clean —
                callers should use :meth:`get` first if they need
                idempotency).
        """
        if not outbox_id or not isinstance(outbox_id, str):
            raise ValueError("outbox_id must be non-empty str")
        resolved_ledger_id = ledger_id or outbox_id
        if resolved_ledger_id in self._records:
            raise ValueError(
                f"ledger row already exists for ledger_id "
                f"{resolved_ledger_id} (outbox_id={outbox_id})"
            )
        now = time.time_ns()
        history_entry: dict[str, Any] = {
            "state": JobLedgerState.QUEUED.value,
            "ts_ns": now,
        }
        rec = JobLedgerRecord(
            ledger_id=resolved_ledger_id,
            outbox_id=outbox_id,
            dataset_id=dataset_id,
            state=JobLedgerState.QUEUED,
            created_at_ns=now,
            updated_at_ns=now,
            history=(history_entry,),
        )
        return self._store(rec)

    def update_state(
        self,
        ledger_id: str,
        new_state: JobLedgerState,
        *,
        runpod_job_id: str | None = None,
        dataset_id: str | None = None,
        artifact_id: str | None = None,
        cost_cents: int | None = None,
        duration_seconds: float | None = None,
        retries: int | None = None,
        note: str | None = None,
    ) -> JobLedgerRecord:
        """Transition a ledger row to ``new_state`` and update fields.

        Appends a history entry recording the transition. Only non-None
        fields are updated (existing values are preserved otherwise).

        Raises:
            KeyError: if ``ledger_id`` is unknown.
        """
        existing = self._records.get(ledger_id)
        if existing is None:
            raise KeyError(f"unknown ledger_id: {ledger_id}")

        now = time.time_ns()
        history_entry: dict[str, Any] = {
            "state": new_state.value,
            "ts_ns": now,
        }
        if runpod_job_id is not None:
            history_entry["runpod_job_id"] = runpod_job_id
        if dataset_id is not None:
            history_entry["dataset_id"] = dataset_id
        if artifact_id is not None:
            history_entry["artifact_id"] = artifact_id
        if cost_cents is not None:
            history_entry["cost_cents"] = cost_cents
        if duration_seconds is not None:
            history_entry["duration_seconds"] = duration_seconds
        if retries is not None:
            history_entry["retries"] = retries
        if note is not None:
            history_entry["note"] = note

        new_history = (*existing.history, history_entry)
        # For terminal rows, keep the terminal state but still append the
        # history entry (audit). Non-terminal transitions update state.
        next_state = new_state if existing.state not in _TERMINAL_STATES else existing.state
        updated = existing.model_copy(
            update={
                "state": next_state,
                "updated_at_ns": now,
                "runpod_job_id": runpod_job_id or existing.runpod_job_id,
                "dataset_id": dataset_id or existing.dataset_id,
                "artifact_id": artifact_id or existing.artifact_id,
                "cost_cents": cost_cents if cost_cents is not None else existing.cost_cents,
                "duration_seconds": duration_seconds
                if duration_seconds is not None
                else existing.duration_seconds,
                "retries": retries if retries is not None else existing.retries,
                "history": new_history,
            }
        )
        return self._store(updated)

    def record_callback(
        self,
        ledger_id: str,
        callback_id: str,
    ) -> JobLedgerRecord:
        """Record that a callback was received for this job.

        Appends ``callback_id`` to ``callbacks`` and transitions the row
        to ``CALLBACK_RECEIVED`` (unless already terminal). Idempotent:
        a duplicate ``callback_id`` is not re-appended (but a history
        entry is still recorded so the audit trail shows the duplicate).

        Raises:
            KeyError: if ``ledger_id`` is unknown.
            ValueError: if ``callback_id`` is empty.
        """
        if not callback_id or not isinstance(callback_id, str):
            raise ValueError("callback_id must be non-empty str")
        existing = self._records.get(ledger_id)
        if existing is None:
            raise KeyError(f"unknown ledger_id: {ledger_id}")

        now = time.time_ns()
        is_dup = callback_id in existing.callbacks
        new_callbacks = existing.callbacks if is_dup else (*existing.callbacks, callback_id)
        history_entry: dict[str, Any] = {
            "state": JobLedgerState.CALLBACK_RECEIVED.value,
            "ts_ns": now,
            "callback_id": callback_id,
            "duplicate": is_dup,
        }
        next_state = (
            existing.state
            if existing.state in _TERMINAL_STATES
            else JobLedgerState.CALLBACK_RECEIVED
        )
        updated = existing.model_copy(
            update={
                "state": next_state,
                "updated_at_ns": now,
                "callbacks": new_callbacks,
                "history": (*existing.history, history_entry),
            }
        )
        return self._store(updated)

    def record_failure(
        self,
        ledger_id: str,
        error_code: str,
        error_message: str,
        *,
        note: str | None = None,
    ) -> JobLedgerRecord:
        """Record a failure for this job and increment ``retries``.

        Appends a failure record ``{"error_code", "error_message",
        "ts_ns"}`` to ``failures`` and increments ``retries`` by 1. Does
        NOT change the state (the caller decides whether to transition
        to ``FAILED`` via :meth:`update_state`); this only records the
        failure event for the audit trail.

        Raises:
            KeyError: if ``ledger_id`` is unknown.
            ValueError: if ``error_code`` is empty.
        """
        if not error_code or not isinstance(error_code, str):
            raise ValueError("error_code must be non-empty str")
        existing = self._records.get(ledger_id)
        if existing is None:
            raise KeyError(f"unknown ledger_id: {ledger_id}")

        now = time.time_ns()
        failure_entry: dict[str, Any] = {
            "error_code": error_code,
            "error_message": error_message,
            "ts_ns": now,
        }
        history_entry: dict[str, Any] = {
            "state": existing.state.value,
            "ts_ns": now,
            "failure": failure_entry,
        }
        if note is not None:
            history_entry["note"] = note
        updated = existing.model_copy(
            update={
                "updated_at_ns": now,
                "failures": (*existing.failures, failure_entry),
                "retries": existing.retries + 1,
                "history": (*existing.history, history_entry),
            }
        )
        return self._store(updated)

    def record_cost(
        self,
        ledger_id: str,
        cost_cents: int,
        duration_seconds: float,
    ) -> JobLedgerRecord:
        """Record cost and duration for this job.

        ``cost_cents`` is ADDED to the existing cumulative cost (a job
        may incur multiple cost events, e.g. a retry). ``duration_seconds``
        is ADDED to the existing cumulative duration. Appends a history
        entry. Does not change state.

        Raises:
            KeyError: if ``ledger_id`` is unknown.
            ValueError: if ``cost_cents`` is negative.
        """
        if cost_cents < 0:
            raise ValueError("cost_cents must be >= 0")
        if duration_seconds < 0:
            raise ValueError("duration_seconds must be >= 0")
        existing = self._records.get(ledger_id)
        if existing is None:
            raise KeyError(f"unknown ledger_id: {ledger_id}")

        now = time.time_ns()
        history_entry: dict[str, Any] = {
            "state": existing.state.value,
            "ts_ns": now,
            "cost_cents_delta": cost_cents,
            "duration_seconds_delta": duration_seconds,
        }
        updated = existing.model_copy(
            update={
                "updated_at_ns": now,
                "cost_cents": existing.cost_cents + cost_cents,
                "duration_seconds": existing.duration_seconds + duration_seconds,
                "history": (*existing.history, history_entry),
            }
        )
        return self._store(updated)

    def record_artifact(
        self,
        ledger_id: str,
        artifact_id: str,
    ) -> JobLedgerRecord:
        """Record a verified artifact and transition to ARTIFACT_VERIFIED.

        Sets ``artifact_id`` and transitions the row to
        ``ARTIFACT_VERIFIED`` (terminal-good). If the row is already in
        a terminal state, the state is preserved but the artifact_id and
        a history entry are still recorded.

        Raises:
            KeyError: if ``ledger_id`` is unknown.
            ValueError: if ``artifact_id`` is empty.
        """
        if not artifact_id or not isinstance(artifact_id, str):
            raise ValueError("artifact_id must be non-empty str")
        existing = self._records.get(ledger_id)
        if existing is None:
            raise KeyError(f"unknown ledger_id: {ledger_id}")

        now = time.time_ns()
        next_state = (
            existing.state
            if existing.state in _TERMINAL_STATES
            else JobLedgerState.ARTIFACT_VERIFIED
        )
        history_entry: dict[str, Any] = {
            "state": JobLedgerState.ARTIFACT_VERIFIED.value,
            "ts_ns": now,
            "artifact_id": artifact_id,
        }
        updated = existing.model_copy(
            update={
                "state": next_state,
                "updated_at_ns": now,
                "artifact_id": artifact_id,
                "history": (*existing.history, history_entry),
            }
        )
        return self._store(updated)

    def get(self, ledger_id: str) -> JobLedgerRecord | None:
        """Return the latest record for ``ledger_id``, or None."""
        return self._records.get(ledger_id)

    def get_by_outbox_id(self, outbox_id: str) -> JobLedgerRecord | None:
        """Return the latest record for ``outbox_id``, or None."""
        return self._by_outbox_id.get(outbox_id)

    def list(
        self,
        *,
        state: JobLedgerState | None = None,
        limit: int = 100,
    ) -> list[JobLedgerRecord]:
        """List records, optionally filtered by state.

        Order is insertion order of the in-memory dict (which reflects
        last-seen order on reload). ``limit`` caps the number returned;
        ``limit <= 0`` returns all.
        """
        records = list(self._records.values())
        if state is not None:
            records = [r for r in records if r.state == state]
        if limit and limit > 0:
            records = records[:limit]
        return records

    def trace(self, ledger_id: str) -> dict[str, Any] | None:
        """Return a full end-to-end trace for one job.

        The trace includes the latest ledger record, the full transition
        history, and all linked identifiers (outbox_id, runpod_job_id,
        dataset_id, artifact_id, callbacks, failures, cost, retries).
        This lets an operator trace one job end-to-end without reading
        logs.

        Returns ``None`` if ``ledger_id`` is unknown.
        """
        rec = self._records.get(ledger_id)
        if rec is None:
            return None
        data = rec.model_dump()
        # Add a convenience summary of the state trajectory.
        trajectory = [{"state": h.get("state"), "ts_ns": h.get("ts_ns")} for h in rec.history]
        data["trajectory"] = trajectory
        data["terminal"] = rec.state in _TERMINAL_STATES
        return data
