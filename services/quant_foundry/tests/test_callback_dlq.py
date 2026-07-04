"""Tests for the callback dead-letter queue (Phase 6 / T-6.2).

Covers:
- DLQRejectionReason enum completeness.
- DLQRecord model integrity (frozen, extra=forbid).
- CallbackDLQ enqueue / idempotency / duplicate detection.
- Security invariant: SIGNATURE_FAILED and PAYLOAD_TAMPER are NEVER
  retryable even if the caller requests is_retryable=True.
- Exponential backoff computation + cap.
- record_retry increments + schedules next retry.
- mark_terminal clears retryability.
- get_retryable_due returns only due, retryable, under-max entries.
- Gateway integration: bad signature, payload tamper, duplicate callback,
  unknown job all land in the DLQ when self.dlq is set.
- Backward compatibility: DLQ disabled (None) → no behavior change.
- Duplicate callback does not double-promote / double-verify.
"""

from __future__ import annotations

import pathlib
import time

import pytest
from quant_foundry.callback_dlq import (
    CallbackDLQ,
    DLQRecord,
    DLQRejectionReason,
)
from quant_foundry.gateway import QuantFoundryGateway
from quant_foundry.outbox import JobStatus
from quant_foundry.schemas import Authority, RunPodCallbackEnvelope, ShadowPrediction
from quant_foundry.signatures import sign_callback

# --- enum / model -----------------------------------------------------------


def test_rejection_reason_enum_has_all_required_values() -> None:
    """All spec-required rejection reasons are present."""
    expected = {
        "signature_failed",
        "missing_required_fields",
        "artifact_verify_failed",
        "duplicate_callback",
        "stale_manifest",
        "payload_tamper",
        "invalid_schema",
        "job_id_mismatch",
        "domain_effect_failed",
    }
    actual = {r.value for r in DLQRejectionReason}
    assert actual == expected


def test_dlq_record_is_frozen_and_extra_forbid() -> None:
    rec = DLQRecord(
        dlq_id="dlq:1",
        job_id="job-1",
        manifest_hash="mh-1",
        idempotency_key="job-1:mh-1",
        rejection_reason=DLQRejectionReason.SIGNATURE_FAILED,
        rejection_detail="bad sig",
        is_retryable=False,
        created_at_ns=1,
        updated_at_ns=1,
    )
    with pytest.raises(Exception):
        rec.dlq_id = "changed"  # type: ignore[misc]
    with pytest.raises(Exception):
        DLQRecord(
            dlq_id="dlq:1",
            job_id="job-1",
            manifest_hash="mh-1",
            idempotency_key="job-1:mh-1",
            rejection_reason=DLQRejectionReason.SIGNATURE_FAILED,
            rejection_detail="bad sig",
            is_retryable=False,
            created_at_ns=1,
            updated_at_ns=1,
            bogus_field="no",  # type: ignore[call-arg]
        )


# --- enqueue / idempotency --------------------------------------------------


def test_enqueue_creates_record_with_idempotency_key(tmp_path: pathlib.Path) -> None:
    dlq = CallbackDLQ(base_dir=tmp_path)
    rec = dlq.enqueue(
        "job-1",
        manifest_hash="mh-1",
        rejection_reason=DLQRejectionReason.SIGNATURE_FAILED,
        rejection_detail="bad sig",
        is_retryable=False,
    )
    assert rec.job_id == "job-1"
    assert rec.manifest_hash == "mh-1"
    assert rec.idempotency_key == "job-1:mh-1"
    assert rec.rejection_reason == DLQRejectionReason.SIGNATURE_FAILED
    assert rec.is_retryable is False
    assert rec.retry_count == 0
    assert rec.dlq_id.startswith("dlq:job-1:")
    assert len(rec.history) == 1
    assert rec.history[0]["event"] == "enqueued"


def test_enqueue_is_idempotent_on_same_key(tmp_path: pathlib.Path) -> None:
    dlq = CallbackDLQ(base_dir=tmp_path)
    rec1 = dlq.enqueue(
        "job-1",
        manifest_hash="mh-1",
        rejection_reason=DLQRejectionReason.DUPLICATE_CALLBACK,
        rejection_detail="dup",
        is_retryable=False,
    )
    rec2 = dlq.enqueue(
        "job-1",
        manifest_hash="mh-1",
        rejection_reason=DLQRejectionReason.DUPLICATE_CALLBACK,
        rejection_detail="dup again",
        is_retryable=False,
    )
    assert rec1.dlq_id == rec2.dlq_id
    assert len(dlq.list(limit=0)) == 1


def test_is_duplicate_detects_existing_key(tmp_path: pathlib.Path) -> None:
    dlq = CallbackDLQ(base_dir=tmp_path)
    assert dlq.is_duplicate("job-1", "mh-1") is False
    dlq.enqueue(
        "job-1",
        manifest_hash="mh-1",
        rejection_reason=DLQRejectionReason.SIGNATURE_FAILED,
        rejection_detail="bad sig",
        is_retryable=False,
    )
    assert dlq.is_duplicate("job-1", "mh-1") is True
    assert dlq.is_duplicate("job-2", "mh-1") is False


def test_get_by_idempotency_returns_record(tmp_path: pathlib.Path) -> None:
    dlq = CallbackDLQ(base_dir=tmp_path)
    rec = dlq.enqueue(
        "job-1",
        manifest_hash="mh-1",
        rejection_reason=DLQRejectionReason.SIGNATURE_FAILED,
        rejection_detail="bad sig",
        is_retryable=False,
    )
    found = dlq.get_by_idempotency("job-1:mh-1")
    assert found is not None
    assert found.dlq_id == rec.dlq_id
    assert dlq.get_by_idempotency("nope:nope") is None


# --- security invariant: never retryable ------------------------------------


def test_signature_failed_never_retryable_even_if_requested(
    tmp_path: pathlib.Path,
) -> None:
    dlq = CallbackDLQ(base_dir=tmp_path)
    rec = dlq.enqueue(
        "job-1",
        manifest_hash="mh-1",
        rejection_reason=DLQRejectionReason.SIGNATURE_FAILED,
        rejection_detail="bad sig",
        is_retryable=True,  # caller requests retryable
    )
    assert rec.is_retryable is False  # but security invariant wins
    assert rec.next_retry_at_ns is None


def test_payload_tamper_never_retryable_even_if_requested(
    tmp_path: pathlib.Path,
) -> None:
    dlq = CallbackDLQ(base_dir=tmp_path)
    rec = dlq.enqueue(
        "job-1",
        manifest_hash="mh-1",
        rejection_reason=DLQRejectionReason.PAYLOAD_TAMPER,
        rejection_detail="tamper",
        is_retryable=True,
    )
    assert rec.is_retryable is False
    assert rec.next_retry_at_ns is None


def test_stale_manifest_is_retryable(tmp_path: pathlib.Path) -> None:
    dlq = CallbackDLQ(base_dir=tmp_path)
    rec = dlq.enqueue(
        "job-1",
        manifest_hash="mh-1",
        rejection_reason=DLQRejectionReason.STALE_MANIFEST,
        rejection_detail="stale",
        is_retryable=True,
    )
    assert rec.is_retryable is True
    assert rec.next_retry_at_ns is not None
    assert rec.next_retry_at_ns > time.time_ns()


# --- backoff ----------------------------------------------------------------


def test_compute_backoff_exponential_capped() -> None:
    base = 1.0
    assert CallbackDLQ.compute_backoff(0, base) == 1.0
    assert CallbackDLQ.compute_backoff(1, base) == 2.0
    assert CallbackDLQ.compute_backoff(2, base) == 4.0
    assert CallbackDLQ.compute_backoff(3, base) == 8.0
    # Cap at 300s
    assert CallbackDLQ.compute_backoff(20, base) == 300.0
    assert CallbackDLQ.compute_backoff(100, base) == 300.0


def test_compute_backoff_zero_base() -> None:
    assert CallbackDLQ.compute_backoff(5, 0.0) == 0.0
    assert CallbackDLQ.compute_backoff(5, -1.0) == 0.0


# --- record_retry / mark_terminal / get_retryable_due -----------------------


def test_record_retry_increments_and_schedules(tmp_path: pathlib.Path) -> None:
    dlq = CallbackDLQ(base_dir=tmp_path)
    rec = dlq.enqueue(
        "job-1",
        manifest_hash="mh-1",
        rejection_reason=DLQRejectionReason.STALE_MANIFEST,
        rejection_detail="stale",
        is_retryable=True,
        max_retries=3,
        backoff_base_seconds=0.01,
    )
    assert rec.retry_count == 0
    r1 = dlq.record_retry(rec.dlq_id)
    assert r1.retry_count == 1
    assert r1.next_retry_at_ns is not None
    assert r1.is_retryable is True
    # History has enqueued + retry_scheduled
    assert len(r1.history) == 2
    assert r1.history[-1]["event"] == "retry_scheduled"


def test_record_retry_exhausted_marks_terminal(tmp_path: pathlib.Path) -> None:
    dlq = CallbackDLQ(base_dir=tmp_path)
    rec = dlq.enqueue(
        "job-1",
        manifest_hash="mh-1",
        rejection_reason=DLQRejectionReason.STALE_MANIFEST,
        rejection_detail="stale",
        is_retryable=True,
        max_retries=2,
        backoff_base_seconds=0.01,
    )
    r1 = dlq.record_retry(rec.dlq_id)  # retry_count=1
    assert r1.is_retryable is True
    r2 = dlq.record_retry(rec.dlq_id)  # retry_count=2 == max_retries
    assert r2.retry_count == 2
    assert r2.is_retryable is False
    assert r2.next_retry_at_ns is None
    assert r2.history[-1]["event"] == "retry_exhausted"


def test_record_retry_non_retryable_raises(tmp_path: pathlib.Path) -> None:
    dlq = CallbackDLQ(base_dir=tmp_path)
    rec = dlq.enqueue(
        "job-1",
        manifest_hash="mh-1",
        rejection_reason=DLQRejectionReason.SIGNATURE_FAILED,
        rejection_detail="bad sig",
        is_retryable=False,
    )
    with pytest.raises(ValueError, match="not retryable"):
        dlq.record_retry(rec.dlq_id)


def test_record_retry_unknown_raises(tmp_path: pathlib.Path) -> None:
    dlq = CallbackDLQ(base_dir=tmp_path)
    with pytest.raises(KeyError):
        dlq.record_retry("nope")


def test_mark_terminal_clears_retryability(tmp_path: pathlib.Path) -> None:
    dlq = CallbackDLQ(base_dir=tmp_path)
    rec = dlq.enqueue(
        "job-1",
        manifest_hash="mh-1",
        rejection_reason=DLQRejectionReason.STALE_MANIFEST,
        rejection_detail="stale",
        is_retryable=True,
    )
    assert rec.is_retryable is True
    term = dlq.mark_terminal(rec.dlq_id)
    assert term.is_retryable is False
    assert term.next_retry_at_ns is None
    assert term.history[-1]["event"] == "marked_terminal"


def test_mark_terminal_unknown_raises(tmp_path: pathlib.Path) -> None:
    dlq = CallbackDLQ(base_dir=tmp_path)
    with pytest.raises(KeyError):
        dlq.mark_terminal("nope")


def test_get_retryable_due_returns_only_due(tmp_path: pathlib.Path) -> None:
    dlq = CallbackDLQ(base_dir=tmp_path)
    # Retryable with a past next_retry_at_ns (set via backoff_base=0 + already due)
    rec_due = dlq.enqueue(
        "job-due",
        manifest_hash="mh-due",
        rejection_reason=DLQRejectionReason.STALE_MANIFEST,
        rejection_detail="stale",
        is_retryable=True,
        backoff_base_seconds=0.0,  # next_retry_at_ns = now + 0
    )
    # Non-retryable
    dlq.enqueue(
        "job-sec",
        manifest_hash="mh-sec",
        rejection_reason=DLQRejectionReason.SIGNATURE_FAILED,
        rejection_detail="bad sig",
        is_retryable=False,
    )
    due = dlq.get_retryable_due()
    assert len(due) == 1
    assert due[0].dlq_id == rec_due.dlq_id


def test_get_retryable_due_excludes_exhausted(tmp_path: pathlib.Path) -> None:
    dlq = CallbackDLQ(base_dir=tmp_path)
    rec = dlq.enqueue(
        "job-1",
        manifest_hash="mh-1",
        rejection_reason=DLQRejectionReason.STALE_MANIFEST,
        rejection_detail="stale",
        is_retryable=True,
        max_retries=1,
        backoff_base_seconds=0.0,
    )
    # Exhaust the retry → terminal, not due.
    dlq.record_retry(rec.dlq_id)
    assert dlq.get_retryable_due() == []


# --- list / persistence -----------------------------------------------------


def test_list_filters_by_reason_and_retryable(tmp_path: pathlib.Path) -> None:
    dlq = CallbackDLQ(base_dir=tmp_path)
    dlq.enqueue(
        "job-1",
        manifest_hash="mh-1",
        rejection_reason=DLQRejectionReason.SIGNATURE_FAILED,
        rejection_detail="bad sig",
        is_retryable=False,
    )
    dlq.enqueue(
        "job-2",
        manifest_hash="mh-2",
        rejection_reason=DLQRejectionReason.STALE_MANIFEST,
        rejection_detail="stale",
        is_retryable=True,
    )
    assert len(dlq.list(limit=0)) == 2
    assert len(dlq.list(reason=DLQRejectionReason.SIGNATURE_FAILED, limit=0)) == 1
    assert len(dlq.list(retryable=True, limit=0)) == 1
    assert len(dlq.list(retryable=False, limit=0)) == 1
    assert len(dlq.list(limit=1)) == 1


def test_persistence_reload(tmp_path: pathlib.Path) -> None:
    dlq = CallbackDLQ(base_dir=tmp_path)
    dlq.enqueue(
        "job-1",
        manifest_hash="mh-1",
        rejection_reason=DLQRejectionReason.SIGNATURE_FAILED,
        rejection_detail="bad sig",
        is_retryable=False,
    )
    # New instance reloads from disk.
    dlq2 = CallbackDLQ(base_dir=tmp_path)
    assert len(dlq2.list(limit=0)) == 1
    assert dlq2.is_duplicate("job-1", "mh-1") is True


def test_jsonl_file_is_append_only(tmp_path: pathlib.Path) -> None:
    dlq = CallbackDLQ(base_dir=tmp_path)
    dlq.enqueue(
        "job-1",
        manifest_hash="mh-1",
        rejection_reason=DLQRejectionReason.SIGNATURE_FAILED,
        rejection_detail="bad sig",
        is_retryable=False,
    )
    dlq.enqueue(
        "job-2",
        manifest_hash="mh-2",
        rejection_reason=DLQRejectionReason.STALE_MANIFEST,
        rejection_detail="stale",
        is_retryable=True,
    )
    path = tmp_path / CallbackDLQ.FILENAME
    lines = path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2  # append-only, one line per enqueue


# --- gateway integration ----------------------------------------------------


def _inference_payload(job_id: str) -> dict:
    return {
        "schema_version": 1,
        "job_id": job_id,
        "artifact_ref": "artifact:trained",
        "symbols": ["AAPL"],
        "horizons_ns": [3_600_000_000_000],
        "feature_snapshot_ref": "feature-snapshot:live",
        "model_id": "model:qf:infer:dlq:1",
        "decision_time": 1_000,
        "feature_rows": [
            {
                "symbol": "AAPL",
                "event_ts": 900,
                "decision_time": 1_000,
                "features": [
                    {"name": "momentum", "value": 0.25, "observed_at": 990},
                    {"name": "volatility", "value": 0.05, "observed_at": 990},
                ],
            }
        ],
        "expected_features": ["momentum", "volatility"],
    }


def _signed_inference_output(
    job_id: str, *, secret: str, signature_override: str | None = None
) -> dict:
    import time as _time

    prediction = ShadowPrediction(
        prediction_id="pred:dlq:1",
        model_id="model:qf:infer:dlq:1",
        symbol="AAPL",
        ts_event=1_000,
        horizon_ns=3_600_000_000_000,
        direction=0.42,
        confidence=0.74,
        authority=Authority.SHADOW_ONLY,
        p_up=0.61,
        feature_availability={"AAPL": True},
        latency_ms=3.5,
    )
    envelope = RunPodCallbackEnvelope(
        job_id=job_id,
        worker_id="runpod-inference",
        result_type="inference_batch",
        payload={"predictions": [prediction.model_dump(mode="json")]},
    )
    payload = envelope.model_dump_json().encode("utf-8")
    callback_ts = int(_time.time())
    signature = (
        sign_callback(payload, secret=secret, ts=callback_ts, job_id=job_id)
        if signature_override is None
        else signature_override
    )
    return {
        "callback_payload": payload.decode("utf-8"),
        "callback_signature": signature,
        "callback_ts": callback_ts,
    }


def _build_gateway_with_dlq(tmp_path: pathlib.Path, *, secret: str):
    from quant_foundry.runpod_client import DispatchResult, DispatchStatus

    class _Client:
        cost_per_dispatch_cents = 0
        endpoint_id = "infer-endpoint"

        def __init__(self) -> None:
            self.dispatches: list[dict] = []

        def dispatch(self, *, job_id, request_payload, budget_cents) -> DispatchResult:
            rp = f"rp-{len(self.dispatches) + 1}"
            self.dispatches.append({"job_id": job_id, "runpod_job_id": rp})
            return DispatchResult(job_id=job_id, status=DispatchStatus.DISPATCHED, runpod_job_id=rp)

        def check_status(self, runpod_job_id: str) -> dict:
            return {"status": "IN_PROGRESS"}

        def check_health(self) -> dict:
            return {"endpoint_id": self.endpoint_id, "status": "ok"}

    client = _Client()
    gateway = QuantFoundryGateway(
        enabled=True,
        mode="runpod",
        shadow_only=True,
        callback_secret=secret,
        base_dir=tmp_path / "qf",
        runpod_clients={"inference": client},
    )
    # Wire the DLQ (backward-compatible: gateway doesn't construct it by
    # default; tests / operators set it explicitly).
    gateway.dlq = CallbackDLQ(base_dir=tmp_path / "qf" / "dlq")
    return gateway, client


def _create_inference_job(gateway, client, job_id: str) -> str:
    gateway.create_job(
        job_id=job_id,
        job_type="inference",
        idempotency_key=f"idem-{job_id}",
        request_payload=_inference_payload(job_id),
    )
    return client.dispatches[0]["runpod_job_id"]


def test_bad_signature_lands_in_dlq(tmp_path: pathlib.Path) -> None:
    secret = "dlq-badsig-secret"
    gateway, client = _build_gateway_with_dlq(tmp_path, secret=secret)
    job_id = "qf:infer:dlq:badsig:1"
    _create_inference_job(gateway, client, job_id)

    output = _signed_inference_output(job_id, secret=secret, signature_override="0" * 64)
    receipt = gateway.receive_callback(
        job_id=job_id,
        payload=output["callback_payload"].encode("utf-8"),
        signature=output["callback_signature"],
        ts=output["callback_ts"],
        worker_id="runpod-inference",
    )
    assert receipt["ok"] is False
    assert receipt["error_code"] == "bad_signature"
    # DLQ has the bad-signature entry.
    dlq_entries = gateway.dlq.list(limit=0)
    assert len(dlq_entries) == 1
    assert dlq_entries[0].rejection_reason == DLQRejectionReason.SIGNATURE_FAILED
    assert dlq_entries[0].is_retryable is False


def test_payload_tamper_lands_in_dlq(tmp_path: pathlib.Path) -> None:
    secret = "dlq-tamper-secret"
    gateway, client = _build_gateway_with_dlq(tmp_path, secret=secret)
    job_id = "qf:infer:dlq:tamper:1"
    _create_inference_job(gateway, client, job_id)

    # First, a valid callback to seed the inbox.
    output1 = _signed_inference_output(job_id, secret=secret)
    gateway.receive_callback(
        job_id=job_id,
        payload=output1["callback_payload"].encode("utf-8"),
        signature=output1["callback_signature"],
        ts=output1["callback_ts"],
        worker_id="runpod-inference",
    )

    # Now send a DIFFERENT payload with a valid signature for the same job.
    # Build a different envelope (different prediction_id).
    prediction2 = ShadowPrediction(
        prediction_id="pred:dlq:tamper:2",
        model_id="model:qf:infer:dlq:1",
        symbol="AAPL",
        ts_event=1_000,
        horizon_ns=3_600_000_000_000,
        direction=0.99,
        confidence=0.50,
        authority=Authority.SHADOW_ONLY,
        p_up=0.55,
        feature_availability={"AAPL": True},
        latency_ms=2.0,
    )
    envelope2 = RunPodCallbackEnvelope(
        job_id=job_id,
        worker_id="runpod-inference",
        result_type="inference_batch",
        payload={"predictions": [prediction2.model_dump(mode="json")]},
    )
    payload2 = envelope2.model_dump_json().encode("utf-8")
    import time as _time

    ts2 = int(_time.time())
    sig2 = sign_callback(payload2, secret=secret, ts=ts2, job_id=job_id)
    receipt = gateway.receive_callback(
        job_id=job_id,
        payload=payload2,
        signature=sig2,
        ts=ts2,
        worker_id="runpod-inference",
    )
    assert receipt["ok"] is False
    assert receipt["error_code"] == "payload_hash_mismatch"
    tamper_entries = [
        e
        for e in gateway.dlq.list(limit=0)
        if e.rejection_reason == DLQRejectionReason.PAYLOAD_TAMPER
    ]
    assert len(tamper_entries) == 1
    assert tamper_entries[0].is_retryable is False


def test_duplicate_callback_does_not_double_process(tmp_path: pathlib.Path) -> None:
    secret = "dlq-dup-secret"
    gateway, client = _build_gateway_with_dlq(tmp_path, secret=secret)
    job_id = "qf:infer:dlq:dup:1"
    _create_inference_job(gateway, client, job_id)

    output = _signed_inference_output(job_id, secret=secret)
    payload = output["callback_payload"].encode("utf-8")

    # First callback: processed.
    r1 = gateway.receive_callback(
        job_id=job_id,
        payload=payload,
        signature=output["callback_signature"],
        ts=output["callback_ts"],
        worker_id="runpod-inference",
    )
    assert r1["ok"] is True
    assert gateway.outbox.get(job_id).status == JobStatus.COMPLETED
    shadow_count_after_first = len(gateway.shadow_ledger.list())

    # Second identical callback: duplicate, no double-process.
    r2 = gateway.receive_callback(
        job_id=job_id,
        payload=payload,
        signature=output["callback_signature"],
        ts=output["callback_ts"],
        worker_id="runpod-inference",
    )
    assert r2["ok"] is False
    assert r2["error_code"] == "duplicate_callback"
    # Shadow ledger did not grow (no double-promote / double-verify).
    assert len(gateway.shadow_ledger.list()) == shadow_count_after_first
    # DLQ has a duplicate_callback entry.
    dup_entries = [
        e
        for e in gateway.dlq.list(limit=0)
        if e.rejection_reason == DLQRejectionReason.DUPLICATE_CALLBACK
    ]
    assert len(dup_entries) == 1
    assert dup_entries[0].is_retryable is False


def test_unknown_job_lands_in_dlq(tmp_path: pathlib.Path) -> None:
    secret = "dlq-unknown-secret"
    gateway, client = _build_gateway_with_dlq(tmp_path, secret=secret)

    output = _signed_inference_output("qf:infer:dlq:unknown:1", secret=secret)
    receipt = gateway.receive_callback(
        job_id="qf:infer:dlq:unknown:1",
        payload=output["callback_payload"].encode("utf-8"),
        signature=output["callback_signature"],
        ts=output["callback_ts"],
        worker_id="runpod-inference",
    )
    assert receipt["ok"] is False
    assert receipt["error_code"] == "unknown_job"
    entries = [
        e
        for e in gateway.dlq.list(limit=0)
        if e.rejection_reason == DLQRejectionReason.JOB_ID_MISMATCH
    ]
    assert len(entries) == 1
    assert entries[0].is_retryable is False


def test_dlq_disabled_is_backward_compatible(tmp_path: pathlib.Path) -> None:
    """When self.dlq is None, behavior is unchanged (no DLQ recording)."""
    secret = "dlq-none-secret"
    gateway, client = _build_gateway_with_dlq(tmp_path, secret=secret)
    gateway.dlq = None  # explicitly disable
    job_id = "qf:infer:dlq:none:1"
    _create_inference_job(gateway, client, job_id)

    output = _signed_inference_output(job_id, secret=secret, signature_override="0" * 64)
    receipt = gateway.receive_callback(
        job_id=job_id,
        payload=output["callback_payload"].encode("utf-8"),
        signature=output["callback_signature"],
        ts=output["callback_ts"],
        worker_id="runpod-inference",
    )
    # Same behavior as before DLQ integration.
    assert receipt["ok"] is False
    assert receipt["error_code"] == "bad_signature"
    # No DLQ file was created.
    assert not (tmp_path / "qf" / "dlq" / CallbackDLQ.FILENAME).is_file()


def test_retryable_failure_schedules_retry(tmp_path: pathlib.Path) -> None:
    """A retryable DLQ entry schedules a retry with exponential backoff."""
    dlq = CallbackDLQ(base_dir=tmp_path)
    rec = dlq.enqueue(
        "job-stale",
        manifest_hash="mh-stale",
        rejection_reason=DLQRejectionReason.STALE_MANIFEST,
        rejection_detail="manifest is stale",
        is_retryable=True,
        max_retries=3,
        backoff_base_seconds=0.01,
    )
    assert rec.is_retryable is True
    assert rec.next_retry_at_ns is not None
    # Simulate a retry.
    r1 = dlq.record_retry(rec.dlq_id)
    assert r1.retry_count == 1
    assert r1.next_retry_at_ns is not None
    # Backoff for retry 1 = 0.01 * 2^1 = 0.02s
    assert r1.next_retry_at_ns > rec.next_retry_at_ns
