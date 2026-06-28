"""Worker-side status and heartbeat utilities for RunPod containers.

Writes structured JSON status files to the RunPod network volume so the
gateway can detect stale/crashed workers without relying solely on the
RunPod API's pull-based status polling.

The status file is written to ``/runpod-volume/status/{job_id}.json``
(or ``/workspace/status/{job_id}.json`` as fallback). Each update is
an atomic write (temp file + rename) so a crash mid-write leaves the
previous status intact, not a truncated file.

Heartbeat protocol:
  1. ``write_status(job_id, "started", ...)``  — on handler entry
  2. ``write_heartbeat(job_id, ...)``           — every N seconds during training
  3. ``write_status(job_id, "completed", ...)`` — on success
  4. ``write_status(job_id, "failed", ...)``    — on failure

The gateway can scan ``/runpod-volume/status/`` for files whose
``heartbeat_at`` is older than a staleness threshold (e.g. 60s) to
detect crashed workers.
"""

from __future__ import annotations

import contextlib
import json
import pathlib
import time
from typing import Any


def _status_dir() -> pathlib.Path:
    """Return the status directory on the RunPod network volume."""
    for base in ("/runpod-volume", "/workspace"):
        path = pathlib.Path(base) / "status"
        try:
            path.mkdir(parents=True, exist_ok=True)
            return path
        except OSError:
            continue
    # Fallback: temp dir (no persistence, but prevents crash)
    import tempfile

    path = pathlib.Path(tempfile.gettempdir()) / "runpod_status"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _status_path(job_id: str) -> pathlib.Path:
    return _status_dir() / f"{job_id}.json"


def _atomic_write(path: pathlib.Path, data: dict[str, Any]) -> None:
    """Write JSON atomically (temp file + rename)."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def write_status(
    job_id: str,
    status: str,
    *,
    error_code: str | None = None,
    error_summary: str | None = None,
    artifact_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Write a status file for a job. Best-effort — never raises.

    status is one of: "started", "training", "inferring", "completed", "failed".
    """
    payload: dict[str, Any] = {
        "job_id": job_id,
        "status": status,
        "updated_at": time.time(),
        "heartbeat_at": time.time(),
    }
    if error_code:
        payload["error_code"] = error_code
    if error_summary:
        payload["error_summary"] = error_summary
    if artifact_id:
        payload["artifact_id"] = artifact_id
    if extra:
        payload.update(extra)
    with contextlib.suppress(OSError):
        _atomic_write(_status_path(job_id), payload)


def write_heartbeat(
    job_id: str,
    *,
    fold: int | None = None,
    total_folds: int | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Update the heartbeat timestamp for a job. Best-effort."""
    path = _status_path(job_id)
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        existing = {"job_id": job_id, "status": "running"}
    existing["heartbeat_at"] = time.time()
    if fold is not None:
        existing["current_fold"] = fold
    if total_folds is not None:
        existing["total_folds"] = total_folds
    if extra:
        existing.update(extra)
    with contextlib.suppress(OSError):
        _atomic_write(path, existing)


def read_status(job_id: str) -> dict[str, Any] | None:
    """Read the status file for a job. Returns None if not found."""
    path = _status_path(job_id)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def clear_status(job_id: str) -> None:
    """Remove the status file for a job. Best-effort."""
    with contextlib.suppress(OSError):
        _status_path(job_id).unlink(missing_ok=True)


__all__ = [
    "clear_status",
    "read_status",
    "write_heartbeat",
    "write_status",
]
