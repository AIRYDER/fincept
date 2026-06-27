"""Callback signing/verification security tests (TASK-13).

These tests lock down the security invariant that the Fincept-side RunPod
poller NEVER signs callbacks on behalf of an unsigned handler. The only
cryptographic operation allowed on the trusted Fincept side is
``verify_callback``. The legacy ``_compat_sign_callback`` shim was removed
and the poller now fails closed (``missing_runpod_callback_fields``) when a
RunPod completion lacks the required ``callback_payload`` /
``callback_signature`` / ``callback_ts`` fields.
"""

from __future__ import annotations

import time
from typing import Any

import quant_foundry.gateway as gateway_module
from quant_foundry.feature_lake import FeatureRow, FeatureValue
from quant_foundry.gateway import QuantFoundryGateway
from quant_foundry.outbox import JobStatus
from quant_foundry.runpod_client import DispatchResult, DispatchStatus
from quant_foundry.schemas import (
    Authority,
    RunPodCallbackEnvelope,
    ShadowPrediction,
)
from quant_foundry.signatures import MAX_TS_SKEW_SECONDS, sign_callback


class RecordingRunPodClient:
    cost_per_dispatch_cents = 0

    def __init__(self, *, endpoint_id: str) -> None:
        self.endpoint_id = endpoint_id
        self.dispatches: list[dict[str, Any]] = []
        self.statuses: dict[str, dict[str, Any]] = {}

    def dispatch(
        self,
        *,
        job_id: str,
        request_payload: dict[str, Any],
        budget_cents: int | None,
    ) -> DispatchResult:
        runpod_job_id = f"rp-{self.endpoint_id}-{len(self.dispatches) + 1}"
        self.dispatches.append(
            {
                "job_id": job_id,
                "request_payload": request_payload,
                "budget_cents": budget_cents,
                "runpod_job_id": runpod_job_id,
            }
        )
        return DispatchResult(
            job_id=job_id,
            status=DispatchStatus.DISPATCHED,
            runpod_job_id=runpod_job_id,
        )

    def check_status(self, runpod_job_id: str) -> dict[str, Any]:
        return self.statuses.get(runpod_job_id, {"status": "IN_PROGRESS"})

    def check_health(self) -> dict[str, Any]:
        return {"endpoint_id": self.endpoint_id, "status": "ok"}


def _inference_payload(job_id: str, *, decision_time: int = 1_000) -> dict[str, Any]:
    rows = (
        FeatureRow(
            symbol="AAPL",
            event_ts=decision_time - 100,
            decision_time=decision_time,
            features=(
                FeatureValue(name="momentum", value=0.25, observed_at=decision_time - 10),
                FeatureValue(name="volatility", value=0.05, observed_at=decision_time - 10),
            ),
        ),
    )
    feature_rows = [
        {
            "symbol": row.symbol,
            "event_ts": row.event_ts,
            "decision_time": row.decision_time,
            "features": [
                {"name": fv.name, "value": fv.value, "observed_at": fv.observed_at}
                for fv in row.features
            ],
        }
        for row in rows
    ]
    return {
        "schema_version": 1,
        "job_id": job_id,
        "artifact_ref": "artifact:trained",
        "symbols": ["AAPL"],
        "horizons_ns": [3_600_000_000_000],
        "feature_snapshot_ref": "feature-snapshot:live",
        "model_id": "model:qf:infer:signed:1",
        "decision_time": decision_time,
        "feature_rows": feature_rows,
        "expected_features": ["momentum", "volatility"],
    }


def _signed_inference_output(
    job_id: str,
    *,
    secret: str,
    ts: int | None = None,
    signature_override: str | None = None,
) -> dict[str, Any]:
    prediction = ShadowPrediction(
        prediction_id="pred:signed:1",
        model_id="model:qf:infer:signed:1",
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
    callback_ts = int(time.time()) if ts is None else int(ts)
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


def _build_gateway(tmp_path, *, secret: str) -> QuantFoundryGateway:
    inference_client = RecordingRunPodClient(endpoint_id="infer-endpoint")
    gateway = QuantFoundryGateway(
        enabled=True,
        mode="runpod",
        shadow_only=True,
        callback_secret=secret,
        base_dir=tmp_path / "qf",
        runpod_clients={"inference": inference_client},
    )
    return gateway, inference_client


def _create_inference_job(
    gateway: QuantFoundryGateway, inference_client: RecordingRunPodClient, job_id: str
) -> str:
    gateway.create_job(
        job_id=job_id,
        job_type="inference",
        idempotency_key=f"idem-{job_id}",
        request_payload=_inference_payload(job_id),
    )
    return inference_client.dispatches[0]["runpod_job_id"]


def test_no_compat_sign_path(tmp_path, monkeypatch) -> None:
    """The poller must never call ``sign_callback`` on the Fincept side.

    An old unsigned handler shape (``output = {"callback": {...}}``) must be
    marked FAILED with ``missing_runpod_callback_fields`` rather than signed
    on the trusted side. We monkey-patch ``quant_foundry.gateway.sign_callback``
    to record every invocation and assert it is never called.
    """
    secret = "compat-sign-secret"
    gateway, inference_client = _build_gateway(tmp_path, secret=secret)
    job_id = "qf:infer:nocompat:1"
    runpod_job_id = _create_inference_job(gateway, inference_client, job_id)

    # Old unsigned handler shape: a bare "callback" dict with no signed fields.
    inference_client.statuses[runpod_job_id] = {
        "status": "COMPLETED",
        "output": {"callback": {"predictions": []}},
    }

    calls: list[dict[str, Any]] = []

    def _recording_sign_callback(payload, *, secret, ts, job_id):
        calls.append({"job_id": job_id, "ts": ts, "payload_len": len(payload)})
        # Delegate to the real signer so that, if the poller incorrectly
        # tried to use the result, behavior would still be well-defined.
        return sign_callback(payload, secret=secret, ts=ts, job_id=job_id)

    monkeypatch.setattr(gateway_module, "sign_callback", _recording_sign_callback)

    receipts = gateway.poll_runpod_results()

    # The poller must fail closed — never sign on the Fincept side.
    assert calls == [], f"poller must never call sign_callback on the Fincept side; got {calls}"
    assert receipts[0]["ok"] is False
    assert receipts[0]["error_code"] == "missing_runpod_callback_fields"
    assert gateway.outbox.get(job_id).status == JobStatus.FAILED
    assert gateway.outbox.get(job_id).error_code == "missing_runpod_callback_fields"


def test_signed_callback_accepted(tmp_path) -> None:
    """Happy path: a properly signed, in-window callback is accepted."""
    secret = "happy-sign-secret"
    gateway, inference_client = _build_gateway(tmp_path, secret=secret)
    job_id = "qf:infer:happy:1"
    runpod_job_id = _create_inference_job(gateway, inference_client, job_id)

    inference_client.statuses[runpod_job_id] = {
        "status": "COMPLETED",
        "output": _signed_inference_output(job_id, secret=secret),
    }

    receipts = gateway.poll_runpod_results()

    assert receipts[0]["ok"] is True
    assert receipts[0]["result"] == "processed"
    assert gateway.outbox.get(job_id).status == JobStatus.COMPLETED


def test_unsigned_old_shape_marked_failed(tmp_path) -> None:
    """Old unsigned ``{"callback": {...}}`` shape → FAILED missing_runpod_callback_fields."""
    secret = "unsigned-secret"
    gateway, inference_client = _build_gateway(tmp_path, secret=secret)
    job_id = "qf:infer:unsigned:1"
    runpod_job_id = _create_inference_job(gateway, inference_client, job_id)

    inference_client.statuses[runpod_job_id] = {
        "status": "COMPLETED",
        "output": {"callback": {"predictions": []}},
    }

    receipts = gateway.poll_runpod_results()

    assert receipts[0]["ok"] is False
    assert receipts[0]["error_code"] == "missing_runpod_callback_fields"
    rec = gateway.outbox.get(job_id)
    assert rec.status == JobStatus.FAILED
    assert rec.error_code == "missing_runpod_callback_fields"


def test_bad_signature_rejected(tmp_path) -> None:
    """A callback with a bad signature is rejected (fail-closed)."""
    secret = "bad-sig-secret"
    gateway, inference_client = _build_gateway(tmp_path, secret=secret)
    job_id = "qf:infer:badsig:1"
    runpod_job_id = _create_inference_job(gateway, inference_client, job_id)

    inference_client.statuses[runpod_job_id] = {
        "status": "COMPLETED",
        "output": _signed_inference_output(job_id, secret=secret, signature_override="0" * 64),
    }

    receipts = gateway.poll_runpod_results()

    assert receipts[0]["ok"] is False
    assert receipts[0]["error_code"] == "bad_signature"
    # No durable callback record should be created for a bad signature.
    assert gateway.outbox.get(job_id).status != JobStatus.COMPLETED


def test_ts_skew_rejected(tmp_path) -> None:
    """A callback whose ts is outside MAX_TS_SKEW_SECONDS is rejected (replay protection)."""
    secret = "skew-secret"
    gateway, inference_client = _build_gateway(tmp_path, secret=secret)
    job_id = "qf:infer:skew:1"
    runpod_job_id = _create_inference_job(gateway, inference_client, job_id)

    stale_ts = int(time.time()) - (MAX_TS_SKEW_SECONDS + 60)
    inference_client.statuses[runpod_job_id] = {
        "status": "COMPLETED",
        "output": _signed_inference_output(job_id, secret=secret, ts=stale_ts),
    }

    receipts = gateway.poll_runpod_results()

    assert receipts[0]["ok"] is False
    assert receipts[0]["error_code"] == "bad_signature"
    assert gateway.outbox.get(job_id).status != JobStatus.COMPLETED
