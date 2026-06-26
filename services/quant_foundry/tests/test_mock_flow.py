"""
TDD tests for quant_foundry.mock_dispatcher + quant_foundry.callbacks (TASK-0305).

Proves the full Fincept -> worker -> Fincept loop without RunPod, using the
REAL contract pieces from TASK-0302/0303/0304:
  - schemas (RunPodTrainingRequest, RunPodInferenceRequest, RunPodCallbackEnvelope,
    ModelDossier, ArtifactManifest, ShadowPrediction, Authority, JobType)
  - signatures (sign_callback / verify_callback)
  - ids (hash_payload, make_idempotency_key)
  - outbox (JobOutbox) + inbox (CallbackInbox)

Acceptance (from NEXT_STEPS_PLAN TASK-0305):
- A mock training job completes through the real contract.
- A mock shadow prediction batch stores in a shadow-only ledger stub.
- Bad callbacks fail closed.
- No existing Fincept trading stream is touched.
- Failure cases: bad signature, invalid schema, duplicate callback, terminal
  job failure.
"""

from __future__ import annotations

import pathlib

import pytest
from quant_foundry.callbacks import (
    CallbackProcessor,
    DossierStub,
    ShadowLedgerStub,
)
from quant_foundry.ids import make_idempotency_key
from quant_foundry.inbox import CallbackInbox, CallbackStatus
from quant_foundry.mock_dispatcher import MockDispatcher
from quant_foundry.outbox import JobOutbox, JobStatus
from quant_foundry.schemas import Authority

# --- helpers ---------------------------------------------------------------


def _make_outbox_inbox(base: pathlib.Path):
    return JobOutbox(base_dir=base / "outbox"), CallbackInbox(base_dir=base / "inbox")


def _training_request(job_id: str) -> dict:
    return {
        "schema_version": 1,
        "job_id": job_id,
        "dataset_manifest_ref": "ds-manifest-1",
        "model_family": "gbm",
        "search_space": {"n_estimators": [100, 200]},
        "random_seed": 42,
        "hardware_class": "mock-gpu",
        "extra_constraints": {},
    }


def _inference_request(job_id: str) -> dict:
    return {
        "schema_version": 1,
        "job_id": job_id,
        "artifact_ref": "artifact-1",
        "symbols": ["AAPL", "MSFT"],
        "horizons_ns": [3_600_000_000_000],
        "feature_snapshot_ref": "feat-snap-1",
    }


# --- module imports --------------------------------------------------------


def test_mock_dispatcher_and_processor_importable() -> None:
    assert callable(MockDispatcher)
    assert callable(CallbackProcessor)
    assert callable(ShadowLedgerStub)
    assert callable(DossierStub)


def test_no_trading_stream_writer_exists() -> None:
    """Hard invariant: dispatcher and processor MUST NOT have any bus
    producer / sig.predict writer. Shadow output stays in the stub."""
    base = pathlib.Path("/tmp/qf-negative")  # not opened; attribute check only
    ob = JobOutbox(base_dir=base / "outbox")
    ib = CallbackInbox(base_dir=base / "inbox")
    disp = MockDispatcher(outbox=ob, inbox=ib, callback_secret="s", base_dir=base)
    proc = CallbackProcessor(
        outbox=ob,
        inbox=ib,
        callback_secret="s",
        shadow_ledger=ShadowLedgerStub(),
        dossier_store=DossierStub(),
    )
    # No bus / trading-stream attributes may exist.
    for attr in ("bus", "producer", "sig_predict_writer", "order_writer", "trading_stream"):
        assert not hasattr(disp, attr), f"dispatcher must not have {attr}"
        assert not hasattr(proc, attr), f"processor must not have {attr}"
    # ShadowLedgerStub must not have a bus/producer either.
    stub = ShadowLedgerStub()
    for attr in ("bus", "producer", "sig_predict_writer", "order_writer"):
        assert not hasattr(stub, attr), f"stub must not have {attr}"


# --- happy path: training --------------------------------------------------


def test_mock_training_job_completes_through_real_contract(tmp_path: pathlib.Path) -> None:
    base = tmp_path / "qf"
    ob, ib = _make_outbox_inbox(base)
    secret = "test-secret"
    disp = MockDispatcher(outbox=ob, inbox=ib, callback_secret=secret, base_dir=base)
    shadow = ShadowLedgerStub()
    dossier = DossierStub()
    proc = CallbackProcessor(
        outbox=ob,
        inbox=ib,
        callback_secret=secret,
        shadow_ledger=shadow,
        dossier_store=dossier,
    )

    job_id = "qf:train:ds1:gbm:h1:1"
    idem = make_idempotency_key("training", "ds1", "gbm", "h1", "1")
    req = _training_request(job_id)
    ob.enqueue(
        job_id=job_id,
        job_type="training",
        idempotency_key=idem,
        request_payload=req,
        priority=1,
        budget_cents=100,
    )

    disp_receipt = disp.dispatch(job_id, request_payload=req)
    assert disp_receipt["status"] == JobStatus.CALLBACK_RECEIVED.value

    # Inbox now has a signed callback
    in_rec = ib.get_by_job_id(job_id)
    assert in_rec is not None
    assert in_rec.signature_valid is True

    proc_receipt = proc.process(job_id)
    assert proc_receipt["outbox_status"] == JobStatus.COMPLETED.value
    assert proc_receipt["inbox_status"] == CallbackStatus.PROCESSED.value

    # Dossier stored in stub
    assert len(dossier.list()) == 1
    stored = dossier.list()[0]
    assert stored["model_family"] == "gbm"

    # Outbox terminal
    assert ob.get(job_id).status == JobStatus.COMPLETED
    # Shadow ledger untouched by training
    assert len(shadow.list()) == 0


# --- happy path: inference -> shadow stub ----------------------------------


def test_mock_inference_stores_shadow_predictions_in_stub(tmp_path: pathlib.Path) -> None:
    base = tmp_path / "qf"
    ob, ib = _make_outbox_inbox(base)
    secret = "inf-secret"
    disp = MockDispatcher(outbox=ob, inbox=ib, callback_secret=secret, base_dir=base)
    shadow = ShadowLedgerStub()
    dossier = DossierStub()
    proc = CallbackProcessor(
        outbox=ob,
        inbox=ib,
        callback_secret=secret,
        shadow_ledger=shadow,
        dossier_store=dossier,
    )

    job_id = "qf:infer:ds1:gbm:h1:1"
    idem = make_idempotency_key("inference", "ds1", "gbm", "h1", "1")
    req = _inference_request(job_id)
    ob.enqueue(
        job_id=job_id,
        job_type="inference",
        idempotency_key=idem,
        request_payload=req,
    )

    disp.dispatch(job_id, request_payload=req)
    proc.process(job_id)

    # Shadow predictions stored in stub, all shadow-only authority
    preds = shadow.list()
    assert len(preds) == 2  # one per symbol
    for p in preds:
        assert p["authority"] == Authority.SHADOW_ONLY.value
        assert p["symbol"] in ("AAPL", "MSFT")
    # No dossier stored for inference
    assert len(dossier.list()) == 0
    # Outbox completed
    assert ob.get(job_id).status == JobStatus.COMPLETED


# --- failure: bad signature ------------------------------------------------


def test_bad_signature_callback_fails_closed(tmp_path: pathlib.Path) -> None:
    base = tmp_path / "qf"
    ob, ib = _make_outbox_inbox(base)
    secret = "real-secret"
    shadow = ShadowLedgerStub()
    dossier = DossierStub()
    proc = CallbackProcessor(
        outbox=ob,
        inbox=ib,
        callback_secret=secret,
        shadow_ledger=shadow,
        dossier_store=dossier,
    )

    job_id = "qf:train:bad-sig:1"
    ob.enqueue(
        job_id=job_id,
        job_type="training",
        idempotency_key="k-bad-sig",
        request_payload=b"{}",
    )
    # Simulate a callback arriving with an INVALID signature verdict.
    ib.receive(
        job_id=job_id,
        idempotency_key="k-bad-sig",
        signature_valid=False,
        payload=b'{"job_id": "x"}',
    )

    receipt = proc.process(job_id)
    assert receipt["inbox_status"] == CallbackStatus.REJECTED.value
    assert receipt["outbox_status"] == JobStatus.FAILED.value
    # No domain effect applied
    assert len(dossier.list()) == 0
    assert len(shadow.list()) == 0


# --- failure: invalid schema ----------------------------------------------


def test_invalid_schema_callback_rejected(tmp_path: pathlib.Path) -> None:
    base = tmp_path / "qf"
    ob, ib = _make_outbox_inbox(base)
    secret = "schema-secret"
    shadow = ShadowLedgerStub()
    dossier = DossierStub()
    proc = CallbackProcessor(
        outbox=ob,
        inbox=ib,
        callback_secret=secret,
        shadow_ledger=shadow,
        dossier_store=dossier,
    )

    job_id = "qf:train:bad-schema:1"
    ob.enqueue(
        job_id=job_id,
        job_type="training",
        idempotency_key="k-schema",
        request_payload=b"{}",
    )
    # Write a payload that is valid JSON but NOT a valid RunPodCallbackEnvelope
    # (missing required fields: worker_id, result_type, payload).
    bad_payload = b'{"job_id": "qf:train:bad-schema:1"}'
    safe_name = job_id.replace(":", "_")
    payload_path = base / "payloads" / f"{safe_name}.json"
    payload_path.parent.mkdir(parents=True, exist_ok=True)
    payload_path.write_bytes(bad_payload)
    ib.receive(
        job_id=job_id,
        idempotency_key="k-schema",
        signature_valid=True,
        payload=bad_payload,
        payload_ref=str(payload_path),
    )

    receipt = proc.process(job_id)
    assert receipt["inbox_status"] == CallbackStatus.REJECTED.value
    assert receipt["outbox_status"] == JobStatus.FAILED.value
    assert len(dossier.list()) == 0


# --- duplicate callback idempotent ----------------------------------------


def test_duplicate_callback_is_idempotent(tmp_path: pathlib.Path) -> None:
    base = tmp_path / "qf"
    ob, ib = _make_outbox_inbox(base)
    secret = "dup-secret"
    disp = MockDispatcher(outbox=ob, inbox=ib, callback_secret=secret, base_dir=base)
    shadow = ShadowLedgerStub()
    dossier = DossierStub()
    proc = CallbackProcessor(
        outbox=ob,
        inbox=ib,
        callback_secret=secret,
        shadow_ledger=shadow,
        dossier_store=dossier,
    )

    job_id = "qf:infer:dup:1"
    req = _inference_request(job_id)
    ob.enqueue(
        job_id=job_id,
        job_type="inference",
        idempotency_key="k-dup",
        request_payload=req,
    )
    disp.dispatch(job_id, request_payload=req)
    proc.process(job_id)
    first_count = len(shadow.list())

    # Processing again must NOT add more predictions.
    proc.process(job_id)
    assert len(shadow.list()) == first_count
    assert ob.get(job_id).status == JobStatus.COMPLETED


# --- terminal job failure --------------------------------------------------


def test_terminal_job_failure_records_error(tmp_path: pathlib.Path) -> None:
    base = tmp_path / "qf"
    ob, ib = _make_outbox_inbox(base)
    secret = "fail-secret"
    disp = MockDispatcher(outbox=ob, inbox=ib, callback_secret=secret, base_dir=base)

    job_id = "qf:train:fail:1"
    ob.enqueue(
        job_id=job_id,
        job_type="training",
        idempotency_key="k-fail",
        request_payload=b"{}",
    )
    # Advance to RUNNING first so failure is a mid-flight transition.
    ob.update_status(job_id, JobStatus.RUNNING)
    receipt = disp.dispatch_failure(
        job_id,
        error_code="OOM",
        error_summary="worker ran out of memory",
    )
    assert receipt["status"] == JobStatus.FAILED.value
    rec = ob.get(job_id)
    assert rec.status == JobStatus.FAILED
    assert rec.error_code == "OOM"
    assert "memory" in (rec.error_summary or "")
    # No callback written for a failed job
    assert ib.get_by_job_id(job_id) is None


# --- tamper: request payload hash mismatch on dispatch ---------------------


def test_dispatch_rejects_tampered_request_payload(tmp_path: pathlib.Path) -> None:
    base = tmp_path / "qf"
    ob, ib = _make_outbox_inbox(base)
    secret = "tamper-secret"
    disp = MockDispatcher(outbox=ob, inbox=ib, callback_secret=secret, base_dir=base)

    job_id = "qf:train:tamper:1"
    req = _training_request(job_id)
    ob.enqueue(
        job_id=job_id,
        job_type="training",
        idempotency_key="k-tamper",
        request_payload=req,
    )
    # Tamper: pass a different payload to dispatch than what was enqueued.
    tampered = dict(req)
    tampered["random_seed"] = 999
    with pytest.raises(ValueError, match=r"payload hash mismatch|tamper|security"):
        disp.dispatch(job_id, request_payload=tampered)
