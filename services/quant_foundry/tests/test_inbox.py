"""
TDD tests for quant_foundry.inbox (TASK-0304).

- Durable local JSONL for callbacks
- Idempotent duplicate callbacks (same job + same payload_hash): no duplicate effects
- Security reject on same job_id + DIFFERENT payload_hash
- Signature validity and payload_hash recorded
- Status + processed tracking
- Survives restart
"""

from __future__ import annotations

import pathlib

import pytest

from quant_foundry.inbox import CallbackInbox, InboxRecord, CallbackStatus
from quant_foundry.ids import hash_payload


def test_inbox_module_imports() -> None:
    assert callable(CallbackInbox)
    assert CallbackStatus.RECEIVED


def test_inbox_receive_records_and_idempotent_duplicate(tmp_path: pathlib.Path) -> None:
    base = tmp_path / "qf-inbox"
    ib = CallbackInbox(base_dir=base)
    payload = b'{"dossier": {"id": "d1"}}'
    ph = hash_payload(payload)

    rec1 = ib.receive(
        job_id="j1",
        idempotency_key="k1",
        signature_valid=True,
        payload=payload,
        worker_id="w1",
    )
    assert isinstance(rec1, InboxRecord)
    assert rec1.job_id == "j1"
    assert rec1.payload_hash == ph
    assert rec1.signature_valid is True
    assert rec1.status == CallbackStatus.RECEIVED

    # Duplicate same hash -> idempotent, no error, same or duplicate status
    rec2 = ib.receive(
        job_id="j1",
        idempotency_key="k1",
        signature_valid=True,
        payload=payload,
        worker_id="w1",
    )
    assert rec2.job_id == "j1"
    assert rec2.payload_hash == ph
    # Must not have created duplicate side effects; status reflects dup or processed once
    assert rec2.status in (CallbackStatus.RECEIVED, CallbackStatus.DUPLICATE)


def test_inbox_rejects_diff_payload_for_same_job_as_security_event(tmp_path: pathlib.Path) -> None:
    base = tmp_path / "qf-inbox"
    ib = CallbackInbox(base_dir=base)
    p1 = b"result-v1"
    p2 = b"result-v2-tampered"

    ib.receive(job_id="j-sec", idempotency_key="ksec", signature_valid=True, payload=p1)

    with pytest.raises(ValueError, match="payload hash mismatch|security|different payload"):
        ib.receive(job_id="j-sec", idempotency_key="ksec", signature_valid=True, payload=p2)


def test_inbox_survives_restart(tmp_path: pathlib.Path) -> None:
    base = tmp_path / "qf-inbox"
    ib1 = CallbackInbox(base_dir=base)
    ib1.receive(job_id="j6", idempotency_key="k6", signature_valid=False, payload=b"bad-sig")

    ib2 = CallbackInbox(base_dir=base)
    rec = ib2.get_by_job_id("j6")
    assert rec is not None
    assert rec.signature_valid is False


def test_inbox_mark_processed_and_history(tmp_path: pathlib.Path) -> None:
    base = tmp_path / "qf-inbox"
    ib = CallbackInbox(base_dir=base)
    rec = ib.receive(job_id="j7", idempotency_key="k7", signature_valid=True, payload=b"ok")
    rec2 = ib.mark_processed("j7", status=CallbackStatus.PROCESSED, note="dossier stored")
    assert rec2.processed_at_ns is not None
    assert rec2.status == CallbackStatus.PROCESSED
