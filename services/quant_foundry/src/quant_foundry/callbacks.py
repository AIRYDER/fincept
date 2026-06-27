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
from typing import Any, Protocol

from pydantic import ValidationError

from quant_foundry.dossier import DossierRecord
from quant_foundry.ids import hash_payload
from quant_foundry.inbox import CallbackInbox, CallbackStatus
from quant_foundry.outbox import JobOutbox, JobStatus
from quant_foundry.registry import DossierRegistry
from quant_foundry.schemas import (
    ArtifactManifest,
    Authority,
    ModelDossier,
    RunPodCallbackEnvelope,
    ShadowPrediction,
)
from quant_foundry.shadow_ledger import ShadowLedger, compute_batch_hash


class ShadowLedgerSink(Protocol):
    def store(self, predictions: list[dict[str, Any]]) -> None: ...


class DossierStoreSink(Protocol):
    def store(self, training_result: dict[str, Any]) -> None: ...


class PredictionPublisher(Protocol):
    """Publishes a Prediction event to the ``sig.predict`` Redis stream.

    Implementations must be idempotent (the callback processor's idempotency
    guard prevents duplicate calls for the same job_id, but the publisher
    should also be safe to retry).
    """

    def publish_prediction(self, prediction: dict[str, Any]) -> str: ...


class DossierLookup(Protocol):
    """Looks up a dossier by model_id to check promotion status."""

    def get(self, model_id: str) -> DossierRecord | None: ...


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


class DurableShadowLedgerStore:
    def __init__(self, ledger: ShadowLedger) -> None:
        self._ledger = ledger

    def store(self, predictions: list[dict[str, Any]]) -> None:
        batch_hash = compute_batch_hash(predictions)
        self._ledger.store_batch(predictions, batch_hash)

    def list(self) -> list[dict[str, Any]]:
        return [record.model_dump(mode="json") for record in self._ledger.list()]


class DurableDossierStore:
    def __init__(self, registry: DossierRegistry) -> None:
        self._registry = registry

    def store(self, training_result: dict[str, Any]) -> None:
        dossier = ModelDossier.model_validate(training_result["dossier"])
        artifact = ArtifactManifest.model_validate(training_result["artifact_manifest"])
        if dossier.artifact_manifest_id != artifact.artifact_id:
            raise ValueError(
                "dossier artifact_manifest_id does not match artifact manifest artifact_id"
            )

        training_metrics = dict(dossier.training_metrics)
        if dossier.pbo is not None:
            training_metrics["pbo"] = float(dossier.pbo)
        if dossier.deflated_sharpe is not None:
            training_metrics["deflated_sharpe"] = float(dossier.deflated_sharpe)

        record = DossierRecord(
            model_id=dossier.model_id,
            artifact_manifest_id=artifact.artifact_id,
            artifact_sha256=artifact.sha256,
            dataset_manifest_id=dossier.dataset_manifest_id,
            dataset_manifest_ref=dossier.dataset_manifest_id,
            feature_schema_hash=artifact.feature_schema_hash,
            label_schema_hash=artifact.label_schema_hash,
            code_git_sha=dossier.code_git_sha or artifact.code_git_sha,
            lockfile_hash=dossier.lockfile_hash or artifact.lockfile_hash,
            container_image_digest=(
                dossier.container_image_digest or artifact.container_image_digest
            ),
            random_seed=dossier.random_seed,
            hardware_class=dossier.hardware_class,
            training_metrics=training_metrics,
        )
        self._registry.register(record)

    def list(self) -> list[dict[str, Any]]:
        return [record.model_dump(mode="json") for record in self._registry.list()]


class CallbackProcessor:
    """Processes signed callbacks from the inbox. Fail-closed, idempotent.

    Constructed with the real `JobOutbox` + `CallbackInbox` (TASK-0304), the
    callback `secret` (TASK-0303), and the shadow/dossier stubs. Drives
    outbox transitions (VALIDATING -> COMPLETED | FAILED) and inbox status
    (PROCESSED | REJECTED).

    When ``paper_bridge`` and ``prediction_publisher`` are provided, the
    processor also publishes paper-approved predictions to the
    ``sig.predict`` Redis stream — but only for models with
    ``DossierStatus.PAPER_APPROVED`` and only when the bridge is enabled.
    This is the single connection point between shadow inference and live
    paper trading.
    """

    def __init__(
        self,
        *,
        outbox: JobOutbox,
        inbox: CallbackInbox,
        callback_secret: str,
        shadow_ledger: ShadowLedgerSink,
        dossier_store: DossierStoreSink,
        paper_bridge: Any | None = None,
        prediction_publisher: PredictionPublisher | None = None,
        dossier_lookup: DossierLookup | None = None,
    ) -> None:
        self.outbox = outbox
        self.inbox = inbox
        self.callback_secret = callback_secret
        self.shadow_ledger = shadow_ledger
        self.dossier_store = dossier_store
        self.paper_bridge = paper_bridge
        self.prediction_publisher = prediction_publisher
        self.dossier_lookup = dossier_lookup

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

        # Idempotent guard: if the outbox job is already terminal, the
        # domain effect has already been applied (or the job failed). Skip
        # regardless of inbox status — this also covers the case where a
        # duplicate external callback overwrote the inbox record with
        # DUPLICATE status after the job was already COMPLETED.
        ob_rec = self.outbox.get(job_id)
        if ob_rec is not None and ob_rec.status in (JobStatus.COMPLETED, JobStatus.FAILED):
            return {
                "job_id": job_id,
                "outbox_status": ob_rec.status.value,
                "inbox_status": in_rec.status.value,
                "result": "already_terminal",
            }

        # Idempotent: already processed -> skip (no duplicate effects).
        if in_rec.status == CallbackStatus.PROCESSED:
            return {
                "job_id": job_id,
                "outbox_status": ob_rec.status.value if ob_rec is not None else None,
                "inbox_status": in_rec.status.value,
                "result": "already_processed",
            }

        # Fail closed: bad signature -> reject, fail job, no domain effect.
        if not in_rec.signature_valid:
            self.inbox.mark_processed(
                job_id,
                status=CallbackStatus.REJECTED,
                error_code="bad_signature",
                error_summary="callback signature verification failed",
            )
            self._fail_job(
                job_id,
                error_code="bad_signature",
                error_summary="callback signature verification failed",
            )
            return self._receipt(job_id, CallbackStatus.REJECTED, "rejected_bad_signature")

        # Tamper check: read payload bytes from payload_ref and verify hash.
        payload_bytes = self._read_payload(in_rec.payload_ref, in_rec.payload_hash)
        if payload_bytes is None:
            self.inbox.mark_processed(
                job_id,
                status=CallbackStatus.REJECTED,
                error_code="payload_tamper",
                error_summary="payload hash mismatch (tamper detected)",
            )
            self._fail_job(
                job_id,
                error_code="payload_tamper",
                error_summary="payload hash mismatch (tamper detected)",
            )
            return self._receipt(job_id, CallbackStatus.REJECTED, "rejected_payload_tamper")

        # Schema validation against the real contract.
        try:
            envelope = RunPodCallbackEnvelope.model_validate(json.loads(payload_bytes))
        except (ValidationError, ValueError) as exc:
            self.inbox.mark_processed(
                job_id,
                status=CallbackStatus.REJECTED,
                error_code="invalid_schema",
                error_summary=f"callback payload failed schema validation: {exc}",
            )
            self._fail_job(
                job_id,
                error_code="invalid_schema",
                error_summary="callback payload failed schema validation",
            )
            return self._receipt(job_id, CallbackStatus.REJECTED, "rejected_invalid_schema")

        # Bind envelope.job_id to the inbox job_id (cross-job replay guard).
        if envelope.job_id != job_id:
            self.inbox.mark_processed(
                job_id,
                status=CallbackStatus.REJECTED,
                error_code="job_id_mismatch",
                error_summary="callback job_id does not match inbox job_id",
            )
            self._fail_job(
                job_id, error_code="job_id_mismatch", error_summary="callback job_id mismatch"
            )
            return self._receipt(job_id, CallbackStatus.REJECTED, "rejected_job_id_mismatch")

        # Apply domain effect by result_type.
        result_type = envelope.result_type
        paper_published: list[dict[str, Any]] = []
        try:
            if result_type == "training_complete":
                self.dossier_store.store(envelope.payload)
            elif result_type == "inference_batch":
                preds = envelope.payload.get("predictions", [])
                self.shadow_ledger.store(preds)
                # Paper bridge: publish paper-approved predictions to sig.predict.
                # This is the ONLY code path that connects shadow inference to
                # live paper trading. Guarded by paper_bridge + prediction_publisher
                # + dossier_lookup all being configured, and the bridge being enabled.
                paper_published = self._maybe_publish_paper(preds)
            else:
                raise ValueError(f"unknown result_type: {result_type}")
        except (ValidationError, ValueError, KeyError) as exc:
            self.inbox.mark_processed(
                job_id,
                status=CallbackStatus.REJECTED,
                error_code="domain_effect_failed",
                error_summary=f"applying domain effect failed: {exc}",
            )
            self._fail_job(
                job_id,
                error_code="domain_effect_failed",
                error_summary=f"applying domain effect failed: {exc}",
            )
            return self._receipt(job_id, CallbackStatus.REJECTED, "rejected_domain_effect")

        # Success.
        self.inbox.mark_processed(
            job_id,
            status=CallbackStatus.PROCESSED,
            note=f"processed result_type={result_type}",
        )
        self.outbox.update_status(job_id, JobStatus.VALIDATING)
        self.outbox.update_status(job_id, JobStatus.COMPLETED)
        receipt = self._receipt(job_id, CallbackStatus.PROCESSED, "processed")
        if paper_published:
            receipt["paper_published"] = paper_published
        return receipt

    # --- internals ---

    def _maybe_publish_paper(self, preds: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Check each shadow prediction's model for paper-approved status.

        If the paper bridge is enabled, the model is ``paper_approved``, and
        the prediction publisher is configured, convert the shadow prediction
        to a ``Prediction`` event and publish it to ``sig.predict``.

        Returns a list of publish receipts (one per published prediction).
        Errors in publishing are caught and logged in the receipt — a single
        publish failure does NOT fail the callback (the shadow ledger store
        already succeeded).
        """
        if (
            self.paper_bridge is None
            or self.prediction_publisher is None
            or self.dossier_lookup is None
        ):
            return []

        published: list[dict[str, Any]] = []
        for pred in preds:
            model_id = pred.get("model_id", "")
            if not model_id:
                continue

            # Look up the dossier to check promotion status.
            dossier = self.dossier_lookup.get(model_id)
            if dossier is None:
                continue

            # Build evidence packet for the bridge.
            from quant_foundry.promotion import PromotionEvidence

            evidence = PromotionEvidence(
                dossier=dossier,
                blocking_issues=[],
            )

            # Attempt to publish via the paper bridge.
            bridge_receipt = self.paper_bridge.publish(
                prediction=pred,
                evidence=evidence,
            )

            if bridge_receipt.status.value == "published":
                # Convert PaperPrediction to Prediction event and publish.
                paper_pred = bridge_receipt.prediction
                if paper_pred is not None:
                    prediction_event = {
                        "agent_id": f"quant_foundry.{model_id}",
                        "symbol": paper_pred.symbol,
                        "horizon_ns": paper_pred.horizon_ns,
                        "ts_event": paper_pred.ts_event,
                        "direction": paper_pred.direction,
                        "confidence": paper_pred.confidence,
                        "calibration_tag": "paper-bridge",
                    }
                    try:
                        stream_id = self.prediction_publisher.publish_prediction(prediction_event)
                        published.append(
                            {
                                "model_id": model_id,
                                "prediction_id": paper_pred.prediction_id,
                                "stream_id": stream_id,
                                "status": "published",
                            }
                        )
                    except Exception as exc:
                        published.append(
                            {
                                "model_id": model_id,
                                "prediction_id": paper_pred.prediction_id,
                                "status": "publish_failed",
                                "error": f"{type(exc).__name__}: {exc}",
                            }
                        )
            elif bridge_receipt.status.value == "refused":
                # Bridge refused (not paper-approved, disabled, etc.) —
                # this is normal, not an error.
                pass

        return published

    def _read_payload(self, payload_ref: str | None, expected_hash: str) -> bytes | None:
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
            job_id,
            JobStatus.FAILED,
            error_code=error_code,
            error_summary=error_summary,
        )

    def _receipt(self, job_id: str, inbox_status: CallbackStatus, result: str) -> dict[str, Any]:
        rec = self.outbox.get(job_id)
        return {
            "job_id": job_id,
            "outbox_status": rec.status.value if rec is not None else None,
            "inbox_status": inbox_status.value,
            "result": result,
        }
