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

from quant_foundry.signatures import verify_callback

if TYPE_CHECKING:
    import pathlib

    from quant_foundry.callback_metrics import CallbackMetricsStore
    from quant_foundry.callbacks import CallbackProcessor
    from quant_foundry.inbox import CallbackInbox
    from quant_foundry.outbox import JobOutbox


class GatewayCallbackMixin:
    """Callback ingestion methods — extracted from QuantFoundryGateway."""

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
            self.inbox.receive(
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
            return {
                "enabled": True,
                "ok": False,
                "error_code": "payload_hash_mismatch",
                "detail": str(exc),
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
