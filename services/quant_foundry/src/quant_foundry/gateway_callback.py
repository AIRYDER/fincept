"""Callback ingestion mixin for QuantFoundryGateway.

Extracted from gateway.py to reduce file complexity. Handles:
  - HMAC signature verification (fail-closed)
  - Inbox recording (idempotent on hash)
  - Callback processing
  - Payload persistence

This is a mixin — it must be combined with QuantFoundryGateway which
provides: self.enabled, self.callback_metrics_store(), self.outbox,
self.inbox, self.processor, self.callback_secret, self.base_dir.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from quant_foundry.callback_dlq import CallbackDLQ, DLQRejectionReason
from quant_foundry.signatures import verify_callback

if TYPE_CHECKING:
    import pathlib

    from quant_foundry.callback_metrics import CallbackMetricsStore
    from quant_foundry.callbacks import CallbackProcessor
    from quant_foundry.inbox import CallbackInbox
    from quant_foundry.outbox import JobOutbox


class GatewayCallbackMixin:
    """Callback ingestion methods — extracted from QuantFoundryGateway.

    Optional DLQ integration (Phase 6 / T-6.2): when ``self.dlq`` is set to
    a :class:`~quant_foundry.callback_dlq.CallbackDLQ` instance, rejected
    callbacks are enqueued to the DLQ with the appropriate rejection
    reason before the error is returned. When ``self.dlq`` is ``None``
    (the default), the DLQ is disabled and behavior is unchanged
    (backward compatible). Callers (tests, the gateway constructor) set
    ``self.dlq`` to enable DLQ recording.
    """

    # DLQ is disabled by default (backward compatible). Set to a
    # CallbackDLQ instance to enable DLQ recording of rejected callbacks.
    dlq: CallbackDLQ | None = None

    if TYPE_CHECKING:
        enabled: bool
        outbox: JobOutbox
        inbox: CallbackInbox
        processor: CallbackProcessor
        callback_secret: str
        base_dir: pathlib.Path
        _worker_status_dir: pathlib.Path | None

        def callback_metrics_store(self) -> CallbackMetricsStore: ...

    # --- callback ingestion (HMAC auth, NOT bearer) ---

    def receive_callback(
        self,
        *,
        job_id: str,
        payload: bytes,
        signature: str,
        ts: int,
        worker_id: str = "external",
    ) -> dict[str, Any]:
        """Receive an external callback. Verifies HMAC signature FIRST,
        then records in the inbox and processes. Fail-closed on bad
        signature or payload hash mismatch (security event).

        Returns a receipt dict. The caller (route) maps non-OK results to
        HTTP error codes.
        """
        if not self.enabled:
            return {"enabled": False, "detail": "Quant Foundry is disabled"}

        # Record the inbound event first so the rejection rate's
        # denominator reflects every callback we saw (accepted + rejected
        # are recorded below). Best-effort: a metrics write failure must
        # not block the security path, but we do NOT silently swallow it
        # at the store level (the store raises on write failure). Suppress
        # OSError here so a disk error does not turn a bad-signature
        # reject into a 500.
        metrics = self.callback_metrics_store()
        with contextlib.suppress(OSError):
            metrics.record("received", reason_code=None)

        # The job must exist in the outbox (callback for unknown job = reject).
        ob_rec = self.outbox.get(job_id)
        if ob_rec is None:
            with contextlib.suppress(OSError):
                metrics.record("rejected", reason_code="unknown_job")
            # DLQ: unknown job is a job_id mismatch. Non-retryable for
            # safety (a callback for an unknown job is not a transient
            # failure — the job does not exist on the trusted side).
            if self.dlq is not None:
                with contextlib.suppress(Exception):
                    self.dlq.enqueue(
                        job_id,
                        manifest_hash=job_id,
                        rejection_reason=DLQRejectionReason.JOB_ID_MISMATCH,
                        rejection_detail=f"no outbox record for job_id {job_id}",
                        is_retryable=False,
                    )
            return {
                "enabled": True,
                "ok": False,
                "error_code": "unknown_job",
                "detail": f"no outbox record for job_id {job_id}",
            }

        # HMAC verify FIRST (constant-time, skew-checked) via TASK-0303.
        # We verify before touching the inbox so a bad signature never
        # creates a durable record (fail-closed, no side effect).
        signature_valid = verify_callback(
            payload,
            signature,
            secret=self.callback_secret,
            ts=ts,
            job_id=job_id,
        )

        if not signature_valid:
            # Bad signature: reject immediately without recording in the
            # inbox. No domain effect, no durable trace of the bad payload.
            with contextlib.suppress(OSError):
                metrics.record("rejected", reason_code="bad_signature")
            # DLQ: bad signature lands in the DLQ for audit. NEVER
            # retryable (security invariant) — the payload is never
            # re-processed. The manifest_hash is the outbox idempotency
            # key (the trusted-side manifest reference for this job).
            if self.dlq is not None:
                with contextlib.suppress(Exception):
                    self.dlq.enqueue(
                        job_id,
                        manifest_hash=ob_rec.idempotency_key,
                        rejection_reason=DLQRejectionReason.SIGNATURE_FAILED,
                        rejection_detail="callback signature verification failed",
                        is_retryable=False,
                    )
            return {
                "enabled": True,
                "ok": False,
                "error_code": "bad_signature",
                "detail": "callback signature verification failed",
            }

        # Valid signature: record the callback in the inbox (durable,
        # idempotent on hash). The inbox's diff-hash guard is a security
        # feature — if the same job_id already has a callback with a
        # DIFFERENT payload hash, that's a tamper/replay event. We catch
        # it and return a clean security error (no crash).
        try:
            in_rec = self.inbox.receive(
                job_id=job_id,
                idempotency_key=ob_rec.idempotency_key,
                signature_valid=signature_valid,
                payload=payload,
                worker_id=worker_id,
                payload_ref=self._write_callback_payload(job_id, payload),
            )
        except ValueError as exc:
            with contextlib.suppress(OSError):
                metrics.record("rejected", reason_code="payload_hash_mismatch")
            # DLQ: payload tamper is a security event. NEVER retryable
            # (security invariant) — the tampered payload is never
            # re-processed. Stored for audit only.
            if self.dlq is not None:
                with contextlib.suppress(Exception):
                    self.dlq.enqueue(
                        job_id,
                        manifest_hash=ob_rec.idempotency_key,
                        rejection_reason=DLQRejectionReason.PAYLOAD_TAMPER,
                        rejection_detail=str(exc),
                        is_retryable=False,
                    )
            return {
                "enabled": True,
                "ok": False,
                "error_code": "payload_hash_mismatch",
                "detail": str(exc),
            }

        # Duplicate callback detection: the inbox records a DUPLICATE
        # status when the same job_id + same payload_hash is received
        # again. Enqueue to the DLQ (idempotent — the DLQ's own
        # idempotency key prevents a second DLQ row) and return WITHOUT
        # processing. This guarantees a duplicate callback does not
        # double-promote or double-verify.
        from quant_foundry.inbox import CallbackStatus

        if in_rec.status == CallbackStatus.DUPLICATE:
            with contextlib.suppress(OSError):
                metrics.record("rejected", reason_code="duplicate_callback")
            if self.dlq is not None:
                with contextlib.suppress(Exception):
                    self.dlq.enqueue(
                        job_id,
                        manifest_hash=ob_rec.idempotency_key,
                        rejection_reason=DLQRejectionReason.DUPLICATE_CALLBACK,
                        rejection_detail=(
                            "duplicate callback for job_id "
                            f"{job_id} (same payload hash)"
                        ),
                        callback_id=in_rec.callback_id,
                        is_retryable=False,
                    )
            return {
                "enabled": True,
                "ok": False,
                "error_code": "duplicate_callback",
                "detail": f"duplicate callback for job_id {job_id}",
                "inbox_status": in_rec.status.value,
            }

        # Process the callback (idempotent + fail-closed on schema/etc).
        proc_receipt = self.processor.process(job_id)
        with contextlib.suppress(OSError):
            metrics.record("accepted", reason_code=None)
        # Best-effort cleanup of the worker status file now that the
        # callback has been processed.  This prevents status files from
        # accumulating indefinitely on the network volume.
        if self._worker_status_dir is not None:
            with contextlib.suppress(OSError):
                _status_path = self._worker_status_dir / f"{job_id}.json"
                _status_path.unlink(missing_ok=True)
        return {
            "enabled": True,
            "ok": True,
            "job_id": job_id,
            "inbox_status": proc_receipt["inbox_status"],
            "outbox_status": proc_receipt["outbox_status"],
            "result": proc_receipt["result"],
        }

    def _write_callback_payload(self, job_id: str, payload: bytes) -> str:
        payload_dir = self.base_dir / "payloads"
        payload_dir.mkdir(parents=True, exist_ok=True)
        safe_name = job_id.replace(":", "_").replace("/", "_").replace("\\", "_")
        payload_path = payload_dir / f"{safe_name}.json"
        payload_path.write_bytes(payload)
        return str(payload_path)
