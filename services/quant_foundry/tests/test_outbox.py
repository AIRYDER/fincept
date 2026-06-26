"""
TDD tests for quant_foundry.outbox (TASK-0304).

Per NEXT_STEPS_PLAN + acceptance:
- Local JSONL storage under configurable base_dir (reports/quant-foundry default)
- Enqueue + status transitions (queued -> ... -> completed/failed)
- History recorded on every status change
- Survive process restart (new instance reloads last state per job_id)
- Idempotency via key + payload_hash
- Receipts include job status history
"""

from __future__ import annotations

import json
import pathlib

import pytest
from quant_foundry.ids import hash_payload
from quant_foundry.outbox import JobOutbox, JobStatus, OutboxRecord


def test_outbox_module_imports_and_types() -> None:
    """Module and public API must be importable."""
    assert callable(JobOutbox)
    assert JobStatus.QUEUED
    assert issubclass(OutboxRecord, object)


def test_outbox_enqueue_creates_record_with_history(tmp_path: pathlib.Path) -> None:
    """Enqueue produces initial queued record with history entry."""
    base = tmp_path / "qf-test"
    ob = JobOutbox(base_dir=base)
    rec = ob.enqueue(
        job_id="qf:train:ds1:gbm:h1:1",
        job_type="training",
        idempotency_key="qf:training:ds1:gbm:h1:1",
        request_payload={"model": "gbm", "seed": 42},
        priority=1,
        budget_cents=100,
    )
    assert isinstance(rec, OutboxRecord)
    assert rec.job_id == "qf:train:ds1:gbm:h1:1"
    assert rec.status == JobStatus.QUEUED
    assert rec.request_payload_hash == hash_payload(
        json.dumps({"model": "gbm", "seed": 42}, sort_keys=True).encode()
    )
    assert len(rec.history) >= 1
    assert rec.history[0]["status"] == "queued"
    assert rec.created_at_ns > 0
    assert rec.updated_at_ns >= rec.created_at_ns


def test_outbox_status_transitions_append_history(tmp_path: pathlib.Path) -> None:
    """update_status transitions and appends to history. Receipt has full history."""
    base = tmp_path / "qf-test"
    ob = JobOutbox(base_dir=base)
    ob.enqueue(job_id="j2", job_type="inference", idempotency_key="k2", request_payload=b"{}")
    rec1 = ob.update_status("j2", JobStatus.DISPATCHING, runpod_endpoint_id="ep-1")
    assert rec1.status == JobStatus.DISPATCHING
    assert len(rec1.history) == 2
    ob.update_status("j2", JobStatus.DISPATCHED, runpod_job_id="rp-99")
    rec3 = ob.update_status("j2", JobStatus.COMPLETED)
    assert rec3.status == JobStatus.COMPLETED
    assert len(rec3.history) == 4
    # history order preserved
    statuses = [h["status"] for h in rec3.history]
    assert statuses == ["queued", "dispatching", "dispatched", "completed"]


def test_outbox_survives_restart_and_loads_latest(tmp_path: pathlib.Path) -> None:
    """New JobOutbox instance after 'restart' sees previous state (JSONL durability)."""
    base = tmp_path / "qf-test"
    ob1 = JobOutbox(base_dir=base)
    ob1.enqueue(job_id="j3", job_type="training", idempotency_key="k3", request_payload=None)
    ob1.update_status("j3", JobStatus.RUNNING)

    # Simulate restart
    ob2 = JobOutbox(base_dir=base)
    rec = ob2.get("j3")
    assert rec is not None
    assert rec.status == JobStatus.RUNNING
    assert rec.job_id == "j3"
    # file exists
    assert (base / "outbox.jsonl").is_file()


def test_outbox_list_and_filter_by_status(tmp_path: pathlib.Path) -> None:
    base = tmp_path / "qf-test"
    ob = JobOutbox(base_dir=base)
    ob.enqueue(job_id="j4a", job_type="t", idempotency_key="k4a", request_payload=None)
    ob.enqueue(job_id="j4b", job_type="t", idempotency_key="k4b", request_payload=None)
    ob.update_status("j4b", JobStatus.FAILED)

    all_jobs = ob.list()
    assert len(all_jobs) == 2
    failed = ob.list(status=JobStatus.FAILED)
    assert len(failed) == 1
    assert failed[0].job_id == "j4b"


def test_outbox_rejects_same_job_id_different_payload_hash(tmp_path: pathlib.Path) -> None:
    """Security: different payload for same job_id must be rejected (not silently overwritten)."""
    base = tmp_path / "qf-test"
    ob = JobOutbox(base_dir=base)
    ob.enqueue(job_id="j5", job_type="t", idempotency_key="k5", request_payload=b"v1")
    with pytest.raises(ValueError, match=r"payload hash mismatch|security"):
        ob.enqueue(job_id="j5", job_type="t", idempotency_key="k5", request_payload=b"v2-different")
