"""Tests for runpod/shared/worker_status.py.

Verifies the worker-side heartbeat/status file protocol:
  - write_status creates a JSON file with the expected fields
  - write_heartbeat updates heartbeat_at on an existing file (and
    creates a new file if none exists)
  - read_status returns None for missing jobs and the dict for existing
  - clear_status removes the file
  - write_status with error fields records error_code/error_summary
  - write_status is best-effort and never raises on permission errors

Tests redirect ``_status_dir`` to a pytest ``tmp_path`` so no real
RunPod volume is touched.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

# Add runpod/shared to sys.path so we can import worker_status.
_SHARED_DIR = os.path.join(
    os.path.dirname(__file__), "..", "shared"
)
if os.path.isdir(_SHARED_DIR):
    sys.path.insert(0, _SHARED_DIR)

import worker_status  # noqa: E402
from worker_status import (  # noqa: E402
    clear_status,
    read_status,
    write_heartbeat,
    write_status,
)


@pytest.fixture
def status_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect _status_dir to a tmp_path so tests are isolated."""
    d = tmp_path / "status"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(worker_status, "_status_dir", lambda: d)
    return d


# ---------------------------------------------------------------------------
# write_status
# ---------------------------------------------------------------------------


def test_write_status_creates_json_file_with_correct_fields(
    status_dir: Path,
) -> None:
    """write_status writes a JSON file with job_id, status, timestamps."""
    write_status("job-1", "started")
    path = status_dir / "job-1.json"
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["job_id"] == "job-1"
    assert data["status"] == "started"
    assert "updated_at" in data
    assert "heartbeat_at" in data
    # updated_at and heartbeat_at should be present and numeric
    assert isinstance(data["updated_at"], float)
    assert isinstance(data["heartbeat_at"], float)


def test_write_status_with_error_fields(status_dir: Path) -> None:
    """write_status records error_code and error_summary when provided."""
    write_status(
        "job-err",
        "failed",
        error_code="training_failed",
        error_summary="LightGBM converged early",
    )
    data = read_status("job-err")
    assert data is not None
    assert data["status"] == "failed"
    assert data["error_code"] == "training_failed"
    assert data["error_summary"] == "LightGBM converged early"


def test_write_status_with_artifact_id(status_dir: Path) -> None:
    """write_status records artifact_id on completion."""
    write_status("job-art", "completed", artifact_id="art-12345")
    data = read_status("job-art")
    assert data is not None
    assert data["status"] == "completed"
    assert data["artifact_id"] == "art-12345"


def test_write_status_with_extra_fields(status_dir: Path) -> None:
    """write_status merges extra dict fields into the payload."""
    write_status(
        "job-extra",
        "completed",
        extra={"latency_ms": 42.5, "callback_ts": 1700000000},
    )
    data = read_status("job-extra")
    assert data is not None
    assert data["latency_ms"] == 42.5
    assert data["callback_ts"] == 1700000000


def test_write_status_does_not_raise_on_permission_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """write_status is best-effort and must never raise on OSError."""

    def _raise_oserror() -> Path:
        raise OSError("simulated volume failure")

    monkeypatch.setattr(worker_status, "_status_dir", _raise_oserror)
    # Should not raise.
    write_status("job-boom", "started")


# ---------------------------------------------------------------------------
# write_heartbeat
# ---------------------------------------------------------------------------


def test_write_heartbeat_updates_existing_file(status_dir: Path) -> None:
    """write_heartbeat updates heartbeat_at on an existing status file."""
    write_status("job-hb", "started")
    original = read_status("job-hb")
    assert original is not None
    original_hb = original["heartbeat_at"]

    # Sleep briefly so the new heartbeat timestamp differs.
    import time

    time.sleep(0.01)
    write_heartbeat("job-hb")

    updated = read_status("job-hb")
    assert updated is not None
    assert updated["heartbeat_at"] > original_hb
    # Status should be preserved from the original write.
    assert updated["status"] == "started"


def test_write_heartbeat_creates_file_if_none_exists(status_dir: Path) -> None:
    """write_heartbeat creates a new status file if none exists."""
    write_heartbeat("job-new")
    data = read_status("job-new")
    assert data is not None
    assert data["job_id"] == "job-new"
    assert "heartbeat_at" in data
    # Default status when no prior file exists.
    assert data["status"] == "running"


def test_write_heartbeat_records_fold_progress(status_dir: Path) -> None:
    """write_heartbeat records current_fold and total_folds when given."""
    write_status("job-fold", "training")
    write_heartbeat("job-fold", fold=3, total_folds=5)
    data = read_status("job-fold")
    assert data is not None
    assert data["current_fold"] == 3
    assert data["total_folds"] == 5


def test_write_heartbeat_with_extra_fields(status_dir: Path) -> None:
    """write_heartbeat merges extra fields into the existing payload."""
    write_status("job-hb-extra", "training")
    write_heartbeat("job-hb-extra", extra={"epoch": 10})
    data = read_status("job-hb-extra")
    assert data is not None
    assert data["epoch"] == 10


# ---------------------------------------------------------------------------
# read_status
# ---------------------------------------------------------------------------


def test_read_status_returns_none_for_missing_job(status_dir: Path) -> None:
    """read_status returns None when no status file exists."""
    assert read_status("nonexistent-job") is None


def test_read_status_returns_dict_for_existing_job(status_dir: Path) -> None:
    """read_status returns the parsed dict for an existing job."""
    write_status("job-read", "completed", artifact_id="art-abc")
    data = read_status("job-read")
    assert data is not None
    assert data["job_id"] == "job-read"
    assert data["status"] == "completed"
    assert data["artifact_id"] == "art-abc"


def test_read_status_returns_none_for_corrupt_json(status_dir: Path) -> None:
    """read_status returns None if the status file is corrupt JSON."""
    (status_dir / "job-corrupt.json").write_text("{not valid json", encoding="utf-8")
    assert read_status("job-corrupt") is None


# ---------------------------------------------------------------------------
# clear_status
# ---------------------------------------------------------------------------


def test_clear_status_removes_file(status_dir: Path) -> None:
    """clear_status removes the status file for a job."""
    write_status("job-clear", "completed")
    assert (status_dir / "job-clear.json").exists()
    clear_status("job-clear")
    assert not (status_dir / "job-clear.json").exists()


def test_clear_status_is_noop_for_missing_job(status_dir: Path) -> None:
    """clear_status does not raise when the status file is missing."""
    # Should not raise.
    clear_status("never-existed")


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------


def test_atomic_write_leaves_no_tmp_file(status_dir: Path) -> None:
    """After write_status, no .tmp file should remain."""
    write_status("job-atomic", "started")
    assert (status_dir / "job-atomic.json").exists()
    assert not (status_dir / "job-atomic.tmp").exists()


def test_status_file_is_valid_json(status_dir: Path) -> None:
    """The status file is valid, parseable JSON."""
    write_status("job-json", "started", extra={"foo": "bar"})
    raw = (status_dir / "job-json.json").read_text(encoding="utf-8")
    # Should parse without error.
    parsed: dict[str, Any] = json.loads(raw)
    assert parsed["foo"] == "bar"
