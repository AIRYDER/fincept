"""
quant_foundry.callbacks — Mock callback processor + shadow-only stubs (TASK-0305).

This module is the Fincept-side counterpart to `mock_dispatcher.py`. It reads
signed callbacks from the durable `CallbackInbox` (TASK-0304), verifies them
against the real contract (TASK-0302 schemas, TASK-0303 signatures), and
applies domain effects idempotently.

Critical safety invariants (enforced + tested):
- Fail closed: a callback with `signature_valid=False` is REJECTED, the
  outbox job is FAILED, and NO domain effect is applied.
- Fail closed: a callback whose payload does not validate against
  `RunPodCallbackEnvelope` (or whose nested dossier/predictions fail their
  schemas) is REJECTED.
- Tamper check: payload bytes read from `payload_ref` are hashed and compared
  to the inbox's recorded `payload_hash`. Mismatch -> REJECTED (security).
- Idempotent: processing an already-PROCESSED job is a no-op (no duplicate
  dossier / shadow predictions).
- Shadow-only: inference predictions MUST carry `authority=shadow-only`. The
  processor asserts this before storing. No `sig.predict` write path exists.
- NO bus producer, NO `sig.predict` writer, NO order writer. Shadow output
  stays in the in-process stub. (Hard invariant + negative test.)

The `ShadowLedgerStub` is explicitly a stub per TASK-0305 spec ("shadow-only
ledger stub"). The real shadow ledger is TASK-0402 (Builder 3) and is not
imported here to keep ownership clean.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

from pydantic import ValidationError

from quant_foundry.ids import hash_payload
from quant_foundry.inbox import CallbackInbox, CallbackStatus
from quant_foundry.outbox import JobOutbox, JobStatus
from quant_foundry.schemas import (
    ArtifactManifest,
    Authority,
    ModelDossier,
    RunPodCallbackEnvelope,
    ShadowPrediction,
)


class ShadowLedgerStub:
    """In-process stub for shadow predictions. NO bus, NO sig.predict writer.

    This is deliberately NOT the real ShadowLedger (TASK-0402). It exists only
    to prove the mock inference loop stores shadow-only output somewhere that
    is NOT a trading stream. The real ledger replaces this stub when the
    promotion gate wiring lands.
    """

    def __init__(self, base_dir: pathlib.Path | str | None = None) -> None:
        self._base_dir = pathlib.Path(base_dir) if base_dir is not None else None
        self._records: list[dict[str, Any]] = []
        if self._base_dir is not None:
            self._base_dir.mkdir(parents=True, exist_ok=True)

    def store(self, predictions: list[dict[str, Any]]) -> None:
        """Store a batch of shadow prediction dicts. Asserts shadow-only authority."""
        for p in predictions:
            # Defense in depth: validate via the real schema (extra='forbid').
            sp = ShadowPrediction.model_validate(p)
            if sp.authority != Authority.SHADOW_ONLY:
                raise ValueError(
                    "non-shadow authority in shadow ledger stub: "
                    f"{sp.authority} (security invariant violation)"
                )
            self._records.append(sp.model_dump())
        if self._base_dir is not None:
            path = self._base_dir / "shadow_predictions.jsonl"
            with path.open("a", encoding="utf-8") as fh:
                for p in predictions:
                    fh.write(json.dumps(p, sort_keys=True) + "\n")

    def list(self) -> list[dict[str, Any]]:
        """Return all stored shadow predictions (model_dumped)."""
        return list(self._records)


class DossierStub:
    """In-process stub for training-result dossiers. NO bus, NO trading writer."""

    def __init__(self) -> None:
        self._records: list[dict[str, Any]] = []

    def store(self, training_result: dict[str, Any]) -> None:
        """Store a training-result dict (must include a validated ModelDossier)."""
        # Defense in depth: validate the nested dossier + artifact manifest.
        ModelDossier.model_validate(training_result["dossier"])
        ArtifactManifest.model_validate(training_result["artifact_manifest"])
        self._records.append(training_result)

    def list(self) -> list[dict[str, Any]]:
        return list(self._records)


class CallbackProcessor:
    """Processes signed callbacks from the inbox. Fail-closed, idempotent.

    Constructed with the real `JobOutbox` + `CallbackInbox` (TASK-0304), the
    callback `secret` (TASK-0303), and the shadow/dossier stubs. Drives
    outbox transitions (VALIDATING -> COMPLETED | FAILED) and inbox status
    (PROCESSED | REJECTED).
    """

    def __init__(
        self,
        *,
        outbox: JobOutbox,
        inbox: CallbackInbox,
        callback_secret: str,
        shadow_ledger: ShadowLedgerStub,
        dossier_store: DossierStub,
    ) -> None:
        self.outbox = outbox
        self.inbox = inbox
        self.callback_secret = callback_secret
        self.shadow_ledger = shadow_ledger
        self.dossier_store = dossier_store

    def process(self, job_id: str) -> dict[str, Any]:
        """Process the latest callback for a job. Idempotent + fail-closed."""
        in_rec = self.inbox.get_by_job_id(job_id)
        if in_rec is None:
            # No callback to process. Not a failure — just nothing to do.
            ob_rec = self.outbox.get(job_id)
            return {
                "job_id": job_id,
                "outbox_status": ob_rec.status.value if ob_rec is not None else None,
                "inbox_status": None,
                "result": "no_callback",
            }

        # Idempotent: already processed -> skip (no duplicate effects).
        if in_rec.status == CallbackStatus.PROCESSED:
            ob_rec = self.outbox.get(job_id)
            return {
                "job_id": job_id,
                "outbox_status": ob_rec.status.value if ob_rec is not None else None,
                "inbox_status": in_rec.status.value,
                "result": "already_processed",
            }

        # Fail closed: bad signature -> reject, fail job, no domain effect.
        if not in_rec.signature_valid:
            self.inbox.mark_processed(
                job_id, status=CallbackStatus.REJECTED,
                error_code="bad_signature",
                error_summary="callback signature verification failed",
            )
            self._fail_job(job_id, error_code="bad_signature",
                           error_summary="callback signature verification failed")
            return self._receipt(job_id, CallbackStatus.REJECTED, "rejected_bad_signature")

        # Tamper check: read payload bytes from payload_ref and verify hash.
        payload_bytes = self._read_payload(in_rec.payload_ref, in_rec.payload_hash)
        if payload_bytes is None:
            self.inbox.mark_processed(
                job_id, status=CallbackStatus.REJECTED,
                error_code="payload_tamper",
                error_summary="payload hash mismatch (tamper detected)",
            )
            self._fail_job(job_id, error_code="payload_tamper",
                           error_summary="payload hash mismatch (tamper detected)")
            return self._receipt(job_id, CallbackStatus.REJECTED, "rejected_payload_tamper")

        # Schema validation against the real contract.
        try:
            envelope = RunPodCallbackEnvelope.model_validate(json.loads(payload_bytes))
        except (ValidationError, ValueError) as exc:
            self.inbox.mark_processed(
                job_id, status=CallbackStatus.REJECTED,
                error_code="invalid_schema",
                error_summary=f"callback payload failed schema validation: {exc}",
            )
            self._fail_job(job_id, error_code="invalid_schema",
                           error_summary="callback payload failed schema validation")
            return self._receipt(job_id, CallbackStatus.REJECTED, "rejected_invalid_schema")

        # Bind envelope.job_id to the inbox job_id (cross-job replay guard).
        if envelope.job_id != job_id:
            self.inbox.mark_processed(
                job_id, status=CallbackStatus.REJECTED,
                error_code="job_id_mismatch",
                error_summary="callback job_id does not match inbox job_id",
            )
            self._fail_job(job_id, error_code="job_id_mismatch",
                           error_summary="callback job_id mismatch")
            return self._receipt(job_id, CallbackStatus.REJECTED, "rejected_job_id_mismatch")

        # Apply domain effect by result_type.
        result_type = envelope.result_type
        try:
            if result_type == "training_complete":
                self.dossier_store.store(envelope.payload)
            elif result_type == "inference_batch":
                preds = envelope.payload.get("predictions", [])
                self.shadow_ledger.store(preds)
            else:
                raise ValueError(f"unknown result_type: {result_type}")
        except (ValidationError, ValueError, KeyError) as exc:
            self.inbox.mark_processed(
                job_id, status=CallbackStatus.REJECTED,
                error_code="domain_effect_failed",
                error_summary=f"applying domain effect failed: {exc}",
            )
            self._fail_job(job_id, error_code="domain_effect_failed",
                           error_summary=f"applying domain effect failed: {exc}")
            return self._receipt(job_id, CallbackStatus.REJECTED, "rejected_domain_effect")

        # Success.
        self.inbox.mark_processed(
            job_id, status=CallbackStatus.PROCESSED,
            note=f"processed result_type={result_type}",
        )
        self.outbox.update_status(job_id, JobStatus.VALIDATING)
        self.outbox.update_status(job_id, JobStatus.COMPLETED)
        return self._receipt(job_id, CallbackStatus.PROCESSED, "processed")

    # --- internals ---

    def _read_payload(
        self, payload_ref: str | None, expected_hash: str
    ) -> bytes | None:
        """Read payload bytes from payload_ref and verify hash. None on tamper."""
        if payload_ref is None:
            return None
        try:
            data = pathlib.Path(payload_ref).read_bytes()
        except OSError:
            return None
        if hash_payload(data) != expected_hash:
            return None
        return data

    def _fail_job(self, job_id: str, *, error_code: str, error_summary: str) -> None:
        rec = self.outbox.get(job_id)
        if rec is None:
            return
        if rec.status == JobStatus.FAILED:
            return
        self.outbox.update_status(
            job_id, JobStatus.FAILED,
            error_code=error_code, error_summary=error_summary,
        )

    def _receipt(
        self, job_id: str, inbox_status: CallbackStatus, result: str
    ) -> dict[str, Any]:
        rec = self.outbox.get(job_id)
        return {
            "job_id": job_id,
            "outbox_status": rec.status.value if rec is not None else None,
            "inbox_status": inbox_status.value,
            "result": result,
        }
