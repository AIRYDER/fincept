"""
quant_foundry.mock_dispatcher — Mock dispatcher for the Fincept <-> worker loop (TASK-0305).

Proves the entire Fincept -> worker -> Fincept loop WITHOUT RunPod, using the
REAL contract pieces:
  - schemas (RunPodTrainingRequest / RunPodInferenceRequest / RunPodCallbackEnvelope
    / ModelDossier / ArtifactManifest / ShadowPrediction / Authority / JobType)
  - signatures (sign_callback)
  - ids (hash_payload, make_idempotency_key)
  - outbox (JobOutbox) + inbox (CallbackInbox)

The dispatcher reads a job from the outbox, simulates deterministic work
locally, builds a signed callback envelope, writes the payload to disk
(durable payload_ref), and records the callback in the inbox. Flipping to
RunPod later is a dispatcher-only change — the outbox/inbox/signature
contract stays identical.

Critical invariants (enforced + tested):
- Tamper check: the request payload passed to `dispatch` must hash to the
  same value recorded in the outbox at enqueue time. Mismatch -> ValueError
  (security event). This catches a swapped payload before any "work" runs.
- Deterministic: identical inputs produce identical artifact_id / dossier
  metrics / shadow predictions (derived from the payload hash + seed).
- Shadow-only: inference results carry `authority=shadow-only` always.
- NO bus producer, NO `sig.predict` writer, NO order writer. The dispatcher
  only writes to the outbox, the inbox, and the local payload file.
- Terminal failure path (`dispatch_failure`) transitions the outbox to
  FAILED with error metadata and writes NO callback.
"""

from __future__ import annotations

import json
import pathlib
import time
from typing import Any

from quant_foundry.ids import hash_payload
from quant_foundry.inbox import CallbackInbox
from quant_foundry.outbox import JobOutbox, JobStatus
from quant_foundry.schemas import (
    ArtifactManifest,
    Authority,
    JobType,
    ModelDossier,
    RunPodCallbackEnvelope,
    RunPodInferenceRequest,
    RunPodTrainingRequest,
    ShadowPrediction,
)
from quant_foundry.signatures import sign_callback


def _serialize(payload: Any) -> bytes:
    """Canonical bytes (mirrors outbox._serialize_payload)."""
    if payload is None:
        return b""
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload)
    if isinstance(payload, str):
        return payload.encode("utf-8")
    return json.dumps(payload, sort_keys=True).encode("utf-8")


class MockDispatcher:
    """Local mock dispatcher. Same contract as the future RunPod dispatcher.

    Args:
        outbox: JobOutbox (TASK-0304) — source of jobs.
        inbox: CallbackInbox (TASK-0304) — sink for signed callbacks.
        callback_secret: HMAC secret for sign_callback (TASK-0303).
        base_dir: root for durable payload files (`<base_dir>/payloads/...`).
    """

    def __init__(
        self,
        *,
        outbox: JobOutbox,
        inbox: CallbackInbox,
        callback_secret: str,
        base_dir: pathlib.Path | str,
    ) -> None:
        self.outbox = outbox
        self.inbox = inbox
        self.callback_secret = callback_secret
        self.base_dir = pathlib.Path(base_dir)
        (self.base_dir / "payloads").mkdir(parents=True, exist_ok=True)

    # --- public API ---

    def dispatch(self, job_id: str, *, request_payload: Any) -> dict[str, Any]:
        """Dispatch a job: verify payload, simulate work, write signed callback.

        Raises ValueError if the request payload hash does not match the
        outbox record (tamper / security event). Raises KeyError if the job
        is unknown.
        """
        rec = self.outbox.get(job_id)
        if rec is None:
            raise KeyError(f"unknown job_id: {job_id}")

        # Tamper check: the payload we're about to "send" must match what was
        # recorded in the outbox at enqueue time.
        payload_bytes = _serialize(request_payload)
        payload_hash = hash_payload(payload_bytes)
        if payload_hash != rec.request_payload_hash:
            raise ValueError(
                "payload hash mismatch for job_id "
                f"{job_id}: dispatch payload differs from enqueued payload "
                "(tamper / security event)"
            )

        # Parse the request against the real schema (defense in depth).
        job_type = (
            JobType(rec.job_type) if rec.job_type in JobType._value2member_map_ else rec.job_type
        )
        result_type: str
        if job_type == JobType.TRAINING:
            train_req = RunPodTrainingRequest.model_validate(
                request_payload if isinstance(request_payload, dict) else json.loads(payload_bytes)
            )
            envelope_payload = self._simulate_training(
                job_id,
                rec.idempotency_key,
                train_req,
                payload_hash,
            )
            result_type = "training_complete"
        elif job_type == JobType.INFERENCE:
            infer_req = RunPodInferenceRequest.model_validate(
                request_payload if isinstance(request_payload, dict) else json.loads(payload_bytes)
            )
            envelope_payload = self._simulate_inference(job_id, infer_req, payload_hash)
            result_type = "inference_batch"
        else:
            raise ValueError(f"unsupported job_type for mock dispatch: {rec.job_type}")

        # Drive outbox transitions (queued -> dispatching -> dispatched -> running).
        self.outbox.update_status(job_id, JobStatus.DISPATCHING)
        self.outbox.update_status(job_id, JobStatus.DISPATCHED)
        self.outbox.update_status(job_id, JobStatus.RUNNING)

        # Build the signed callback envelope.
        now_ns = time.time_ns()
        envelope = RunPodCallbackEnvelope(
            job_id=job_id,
            worker_id="mock-worker-1",
            result_type=result_type,
            payload=envelope_payload,
            received_at_ns=now_ns,
        )
        envelope_bytes = envelope.model_dump_json().encode("utf-8")

        # Durably store the payload and record its ref.
        # Sanitize job_id for the filename (colons are illegal on Windows).
        safe_name = job_id.replace(":", "_").replace("/", "_").replace("\\", "_")
        payload_path = self.base_dir / "payloads" / f"{safe_name}.json"
        payload_path.write_bytes(envelope_bytes)

        # Sign the callback (real HMAC path).
        ts = int(time.time())
        signature = sign_callback(
            envelope_bytes,
            secret=self.callback_secret,
            ts=ts,
            job_id=job_id,
        )

        # Record the callback in the inbox (real durability path).
        in_rec = self.inbox.receive(
            job_id=job_id,
            idempotency_key=rec.idempotency_key,
            signature_valid=True,
            payload=envelope_bytes,
            worker_id="mock-worker-1",
            payload_ref=str(payload_path),
        )

        # Outbox acknowledges callback received.
        self.outbox.update_status(job_id, JobStatus.CALLBACK_RECEIVED)

        return {
            "job_id": job_id,
            "status": JobStatus.CALLBACK_RECEIVED.value,
            "callback_id": in_rec.callback_id,
            "result_type": result_type,
            "signature": signature,
            "payload_ref": str(payload_path),
        }

    def dispatch_failure(
        self, job_id: str, *, error_code: str, error_summary: str
    ) -> dict[str, Any]:
        """Transition a job to FAILED with error metadata. Writes NO callback."""
        rec = self.outbox.get(job_id)
        if rec is None:
            raise KeyError(f"unknown job_id: {job_id}")
        self.outbox.update_status(
            job_id,
            JobStatus.FAILED,
            error_code=error_code,
            error_summary=error_summary,
        )
        return {
            "job_id": job_id,
            "status": JobStatus.FAILED.value,
            "error_code": error_code,
            "error_summary": error_summary,
        }

    # --- mock work simulators (deterministic) ---

    def _simulate_training(
        self,
        job_id: str,
        idempotency_key: str,
        req: RunPodTrainingRequest,
        payload_hash: str,
    ) -> dict[str, Any]:
        """Deterministic mock training result: artifact + dossier."""
        now_ns = time.time_ns()
        artifact_id = f"artifact:{payload_hash[:16]}"
        # Deterministic mock metrics derived from the payload hash (stable).
        seed_hex = int(payload_hash[:8], 16)
        pbo = (seed_hex % 100) / 100.0
        deflated_sharpe = ((seed_hex >> 8) % 300) / 100.0 - 1.0
        artifact = ArtifactManifest(
            artifact_id=artifact_id,
            sha256=payload_hash,
            size_bytes=1024 + (seed_hex % 4096),
            uri=None,
            model_family=req.model_family,
            created_at_ns=now_ns,
            feature_schema_hash=payload_hash[:16],
            label_schema_hash=payload_hash[16:32],
            code_git_sha="mock-git-sha",
            lockfile_hash="mock-lockfile-hash",
            container_image_digest="mock-digest",
        )
        dossier = ModelDossier(
            model_id=f"model:{job_id}",
            artifact_manifest_id=artifact.artifact_id,
            dataset_manifest_id=req.dataset_manifest_ref,
            code_git_sha="mock-git-sha",
            lockfile_hash="mock-lockfile-hash",
            container_image_digest="mock-digest",
            random_seed=req.random_seed,
            hardware_class=req.hardware_class,
            training_metrics={"accuracy": 0.5 + (pbo / 2.0), "logloss": 0.7 - (pbo / 4.0)},
            pbo=pbo,
            deflated_sharpe=deflated_sharpe,
            authority=Authority.SHADOW_ONLY,
            metadata={"idempotency_key": idempotency_key},
        )
        return {
            "model_family": req.model_family,
            "dossier": dossier.model_dump(),
            "artifact_manifest": artifact.model_dump(),
        }

    def _simulate_inference(
        self,
        job_id: str,
        req: RunPodInferenceRequest,
        payload_hash: str,
    ) -> dict[str, Any]:
        """Deterministic mock shadow inference batch (one prediction per symbol)."""
        now_ns = time.time_ns()
        predictions: list[dict[str, Any]] = []
        for i, symbol in enumerate(req.symbols):
            seed_hex = int(payload_hash[i * 2 : i * 2 + 8].ljust(8, "0"), 16)
            direction = ((seed_hex % 200) - 100) / 100.0  # [-1.0, 1.0]
            confidence = (seed_hex % 100) / 100.0  # [0.0, 1.0]
            p_up = 0.5 + (direction / 4.0)
            p_up = max(0.0, min(1.0, p_up))
            sp = ShadowPrediction(
                prediction_id=f"pred:{job_id}:{symbol}:{now_ns}",
                model_id=f"model:{job_id}",
                symbol=symbol,
                ts_event=now_ns,
                horizon_ns=req.horizons_ns[0] if req.horizons_ns else 3_600_000_000_000,
                direction=direction,
                confidence=confidence,
                authority=Authority.SHADOW_ONLY,
                expected_return=direction * 0.01,
                p_up=p_up,
                latency_ms=float((seed_hex % 50) + 1),
            )
            predictions.append(sp.model_dump())
        return {"predictions": predictions}
