"""Tests for paper bridge integration in CallbackProcessor.

These tests verify that:
1. When the paper bridge is NOT configured, inference callbacks only store
   to the shadow ledger (backward compatible).
2. When the paper bridge IS configured but the model is NOT paper-approved,
   predictions are NOT published to sig.predict.
3. When the paper bridge IS configured AND the model IS paper-approved AND
   the bridge is enabled, predictions ARE published to sig.predict.
4. When the paper bridge is configured but disabled (allow_paper_bridge=false),
   predictions are NOT published.
5. Publish failures do NOT fail the callback (shadow ledger store already
   succeeded).
6. The receipt includes paper_published info when predictions are published.

These tests use in-memory stubs — no Redis, no filesystem.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from quant_foundry.callbacks import (
    CallbackProcessor,
    ShadowLedgerStub,
    DossierStub,
)
from quant_foundry.dossier import DossierRecord, DossierStatus
from quant_foundry.inbox import CallbackInbox, CallbackStatus
from quant_foundry.outbox import JobOutbox, JobStatus
from quant_foundry.paper_bridge import BridgeConfig, BridgeStatus, PaperBridge
from quant_foundry.schemas import (
    Authority,
    RunPodCallbackEnvelope,
    ShadowPrediction,
)
from quant_foundry.signatures import sign_callback


class FakeDossierLookup:
    """In-memory dossier lookup for testing."""

    def __init__(self, dossiers: dict[str, DossierRecord] | None = None) -> None:
        self._dossiers = dossiers or {}

    def get(self, model_id: str) -> DossierRecord | None:
        return self._dossiers.get(model_id)


class RecordingPublisher:
    """Records all publish calls without actually publishing to Redis."""

    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []
        self.should_fail = False

    def publish_prediction(self, prediction: dict[str, Any]) -> str:
        if self.should_fail:
            raise RuntimeError("simulated publish failure")
        stream_id = f"fake-stream-id-{len(self.published) + 1}"
        self.published.append(prediction)
        return stream_id


def _make_dossier(
    model_id: str = "test-model-v1",
    status: DossierStatus = DossierStatus.PAPER_APPROVED,
) -> DossierRecord:
    """Create a minimal DossierRecord for testing."""
    return DossierRecord(
        model_id=model_id,
        artifact_manifest_id="art-001",
        artifact_sha256="abc123",
        dataset_manifest_id="ds-001",
        feature_schema_hash="feat-hash",
        label_schema_hash="label-hash",
        status=status,
    )


def _make_inference_envelope(
    predictions: list[dict[str, Any]],
    job_id: str = "job-test-001",
) -> RunPodCallbackEnvelope:
    """Create a callback envelope with inference_batch result type."""
    return RunPodCallbackEnvelope(
        job_id=job_id,
        worker_id="test-worker",
        result_type="inference_batch",
        payload={"predictions": predictions},
    )


def _make_shadow_prediction(
    model_id: str = "test-model-v1",
    prediction_id: str = "pred-001",
) -> dict[str, Any]:
    """Create a shadow prediction dict as it would appear in a callback."""
    return {
        "prediction_id": prediction_id,
        "model_id": model_id,
        "symbol": "AAPL",
        "ts_event": 1_700_000_000_000_000_000,
        "horizon_ns": 3_600_000_000_000,
        "direction": 0.75,
        "confidence": 0.82,
        "authority": "shadow-only",
    }


def _setup_processor(
    tmp_path,
    *,
    paper_bridge: PaperBridge | None = None,
    prediction_publisher: Any | None = None,
    dossier_lookup: Any | None = None,
) -> tuple[CallbackProcessor, JobOutbox, CallbackInbox]:
    """Set up a CallbackProcessor with in-memory stubs."""
    base = tmp_path / "qf_test"
    base.mkdir(parents=True, exist_ok=True)
    outbox = JobOutbox(base_dir=base / "outbox")
    inbox = CallbackInbox(base_dir=base / "inbox")

    # Create a job in the outbox.
    outbox.create(
        job_id="job-test-001",
        job_type="inference",
        idempotency_key="idem-001",
        request_payload={"test": True},
    )

    processor = CallbackProcessor(
        outbox=outbox,
        inbox=inbox,
        callback_secret="test-secret",
        shadow_ledger=ShadowLedgerStub(),
        dossier_store=DossierStub(),
        paper_bridge=paper_bridge,
        prediction_publisher=prediction_publisher,
        dossier_lookup=dossier_lookup,
    )
    return processor, outbox, inbox


def _submit_callback(
    processor: CallbackProcessor,
    inbox: CallbackInbox,
    outbox: JobOutbox,
    envelope: RunPodCallbackEnvelope,
    secret: str = "test-secret",
) -> dict[str, Any]:
    """Submit a signed callback to the inbox and process it."""
    job_id = envelope.job_id
    payload = json.dumps(envelope.model_dump()).encode()
    ts = int(time.time())
    signature = sign_callback(payload, secret=secret, ts=ts, job_id=job_id)

    # Record in inbox.
    inbox.receive(
        job_id=job_id,
        idempotency_key="idem-001",
        signature_valid=True,
        payload=payload,
        worker_id="test-worker",
        payload_ref=None,
    )

    # Process.
    return processor.process(job_id)


class TestPaperBridgeNotConfigured:
    """When paper_bridge is None, behavior is unchanged (backward compatible)."""

    def test_inference_callback_stores_shadow_only(self, tmp_path):
        """Without paper bridge, predictions only go to shadow ledger."""
        processor, outbox, inbox = _setup_processor(tmp_path)
        pred = _make_shadow_prediction()
        envelope = _make_inference_envelope([pred])

        receipt = _submit_callback(processor, inbox, outbox, envelope)

        assert receipt["result"] == "processed"
        assert "paper_published" not in receipt
        assert outbox.get("job-test-001").status == JobStatus.COMPLETED


class TestPaperBridgeRefusedNotPaperApproved:
    """When model is NOT paper-approved, bridge refuses and no publish happens."""

    def test_shadow_approved_model_not_published(self, tmp_path):
        """A shadow_approved model should NOT be published to sig.predict."""
        bridge = PaperBridge(
            config=BridgeConfig(allow_paper_bridge=True, runtime_mode="paper")
        )
        publisher = RecordingPublisher()
        dossier = _make_dossier(status=DossierStatus.SHADOW_APPROVED)
        lookup = FakeDossierLookup({"test-model-v1": dossier})

        processor, outbox, inbox = _setup_processor(
            tmp_path,
            paper_bridge=bridge,
            prediction_publisher=publisher,
            dossier_lookup=lookup,
        )

        pred = _make_shadow_prediction()
        envelope = _make_inference_envelope([pred])

        receipt = _submit_callback(processor, inbox, outbox, envelope)

        assert receipt["result"] == "processed"
        assert publisher.published == []
        # Receipt should not have paper_published since nothing was published.
        assert "paper_published" not in receipt or receipt["paper_published"] == []


class TestPaperBridgePublishesPaperApproved:
    """When model IS paper-approved and bridge is enabled, publish to sig.predict."""

    def test_paper_approved_model_published(self, tmp_path):
        """A paper_approved model should be published to sig.predict."""
        bridge = PaperBridge(
            config=BridgeConfig(allow_paper_bridge=True, runtime_mode="paper")
        )
        publisher = RecordingPublisher()
        dossier = _make_dossier(status=DossierStatus.PAPER_APPROVED)
        lookup = FakeDossierLookup({"test-model-v1": dossier})

        processor, outbox, inbox = _setup_processor(
            tmp_path,
            paper_bridge=bridge,
            prediction_publisher=publisher,
            dossier_lookup=lookup,
        )

        pred = _make_shadow_prediction()
        envelope = _make_inference_envelope([pred])

        receipt = _submit_callback(processor, inbox, outbox, envelope)

        assert receipt["result"] == "processed"
        assert len(publisher.published) == 1
        # Verify the prediction event has the correct fields.
        published = publisher.published[0]
        assert published["agent_id"] == "quant_foundry.test-model-v1"
        assert published["symbol"] == "AAPL"
        assert published["direction"] == 0.75
        assert published["confidence"] == 0.82
        assert published["calibration_tag"] == "paper-bridge"
        # Receipt should include paper_published info.
        assert "paper_published" in receipt
        assert receipt["paper_published"][0]["status"] == "published"
        assert receipt["paper_published"][0]["model_id"] == "test-model-v1"

    def test_multiple_predictions_mixed_models(self, tmp_path):
        """Only paper-approved models get published; others are skipped."""
        bridge = PaperBridge(
            config=BridgeConfig(allow_paper_bridge=True, runtime_mode="paper")
        )
        publisher = RecordingPublisher()
        lookup = FakeDossierLookup(
            {
                "paper-model-v1": _make_dossier(
                    model_id="paper-model-v1", status=DossierStatus.PAPER_APPROVED
                ),
                "shadow-model-v1": _make_dossier(
                    model_id="shadow-model-v1", status=DossierStatus.SHADOW_APPROVED
                ),
            }
        )

        processor, outbox, inbox = _setup_processor(
            tmp_path,
            paper_bridge=bridge,
            prediction_publisher=publisher,
            dossier_lookup=lookup,
        )

        preds = [
            _make_shadow_prediction(model_id="paper-model-v1", prediction_id="pred-1"),
            _make_shadow_prediction(model_id="shadow-model-v1", prediction_id="pred-2"),
            _make_shadow_prediction(model_id="unknown-model", prediction_id="pred-3"),
        ]
        envelope = _make_inference_envelope(preds)

        receipt = _submit_callback(processor, inbox, outbox, envelope)

        assert receipt["result"] == "processed"
        # Only the paper-approved model should be published.
        assert len(publisher.published) == 1
        assert publisher.published[0]["agent_id"] == "quant_foundry.paper-model-v1"


class TestPaperBridgeDisabled:
    """When bridge is disabled (allow_paper_bridge=false), no publishing."""

    def test_disabled_bridge_no_publish(self, tmp_path):
        """Even with a paper-approved model, a disabled bridge doesn't publish."""
        bridge = PaperBridge(
            config=BridgeConfig(allow_paper_bridge=False, runtime_mode="paper")
        )
        publisher = RecordingPublisher()
        dossier = _make_dossier(status=DossierStatus.PAPER_APPROVED)
        lookup = FakeDossierLookup({"test-model-v1": dossier})

        processor, outbox, inbox = _setup_processor(
            tmp_path,
            paper_bridge=bridge,
            prediction_publisher=publisher,
            dossier_lookup=lookup,
        )

        pred = _make_shadow_prediction()
        envelope = _make_inference_envelope([pred])

        receipt = _submit_callback(processor, inbox, outbox, envelope)

        assert receipt["result"] == "processed"
        assert publisher.published == []


class TestPaperBridgePublishFailure:
    """Publish failures don't fail the callback."""

    def test_publish_failure_doesnt_fail_callback(self, tmp_path):
        """If the Redis publish fails, the callback should still succeed
        (shadow ledger store already succeeded)."""
        bridge = PaperBridge(
            config=BridgeConfig(allow_paper_bridge=True, runtime_mode="paper")
        )
        publisher = RecordingPublisher()
        publisher.should_fail = True
        dossier = _make_dossier(status=DossierStatus.PAPER_APPROVED)
        lookup = FakeDossierLookup({"test-model-v1": dossier})

        processor, outbox, inbox = _setup_processor(
            tmp_path,
            paper_bridge=bridge,
            prediction_publisher=publisher,
            dossier_lookup=lookup,
        )

        pred = _make_shadow_prediction()
        envelope = _make_inference_envelope([pred])

        receipt = _submit_callback(processor, inbox, outbox, envelope)

        # Callback should still succeed.
        assert receipt["result"] == "processed"
        assert outbox.get("job-test-001").status == JobStatus.COMPLETED
        # But the paper_published receipt should show the failure.
        assert "paper_published" in receipt
        assert receipt["paper_published"][0]["status"] == "publish_failed"


class TestPaperBridgeIdempotency:
    """Re-processing an already-processed callback doesn't re-publish."""

    def test_already_processed_no_republish(self, tmp_path):
        """Processing the same callback twice should only publish once."""
        bridge = PaperBridge(
            config=BridgeConfig(allow_paper_bridge=True, runtime_mode="paper")
        )
        publisher = RecordingPublisher()
        dossier = _make_dossier(status=DossierStatus.PAPER_APPROVED)
        lookup = FakeDossierLookup({"test-model-v1": dossier})

        processor, outbox, inbox = _setup_processor(
            tmp_path,
            paper_bridge=bridge,
            prediction_publisher=publisher,
            dossier_lookup=lookup,
        )

        pred = _make_shadow_prediction()
        envelope = _make_inference_envelope([pred])

        receipt1 = _submit_callback(processor, inbox, outbox, envelope)
        assert receipt1["result"] == "processed"
        assert len(publisher.published) == 1

        # Process again — should be idempotent.
        receipt2 = processor.process("job-test-001")
        assert receipt2["result"] == "already_processed"
        assert len(publisher.published) == 1  # No additional publish
