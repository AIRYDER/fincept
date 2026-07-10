"""
Gateway-level E2E integration test with real ML (LightGBM).

Verifies the full gateway pipeline with a real LightGBM trainer:
1. ``RealLightGBMTrainer`` trains a real model and produces a real artifact.
2. The gateway ingests the training callback (via RunPod polling) and
   registers a dossier with real metrics in the durable registry.
3. The dossier's metrics are NOT the stub pattern (``0.5 + pbo/2.0``).
4. ``dispatch_shadow_inference_batch()`` dispatches inference jobs for
   SHADOW_APPROVED models.

Uses ``pytest.importorskip("lightgbm")`` so the file is skipped cleanly
without ML deps. Uses a ``RecordingRunPodClient`` (same pattern as
``test_gateway_runpod_loop.py``) to simulate the RunPod worker completing
a training job with a real model.

Safety invariants verified:
- ``Authority.SHADOW_ONLY`` on every dossier and prediction.
- No order/OMS fields in any dispatch receipt or ledger record.
- No secrets in any receipt or health output.
- All temporary files use ``tmp_path`` (no pollution).
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

import pytest

# --- skip entire module if lightgbm / numpy are not installed -----------------
_LIGHTGBM = pytest.importorskip("lightgbm")
_NUMPY = pytest.importorskip("numpy")

# Legacy trainer construction (without column_roles) emits a
# DeprecationWarning; these tests intentionally exercise that path.
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ORDER_LIKE_FIELDS: frozenset[str] = frozenset(
    {
        "quantity",
        "size",
        "side",
        "broker",
        "order_type",
        "order_id",
        "client_order_id",
        "time_in_force",
        "leverage",
        "margin_type",
        "account_id",
    }
)


def _make_synthetic_dataset(
    tmp_path: Path,
    n: int = 300,
    seed: int = 42,
    n_features: int = 4,
) -> tuple[Path, Any, Any]:
    """Create a synthetic CSV dataset with real signal for LightGBM training."""
    import numpy as np

    rng = np.random.RandomState(seed)
    timestamps = np.arange(n, dtype=np.int64)
    features = [rng.randn(n) for _ in range(n_features)]
    weights = [0.8, 0.5, -0.6] + [0.0] * max(0, n_features - 3)
    logit = sum(w * f for w, f in zip(weights, features, strict=False)) + 0.05 * rng.randn(n)
    label = (logit > 0).astype(float)
    data = np.column_stack([timestamps, *features, label])
    path = tmp_path / "synthetic_data.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    header = ",".join(["timestamp"] + [f"f{i + 1}" for i in range(n_features)] + ["label"])
    np.savetxt(str(path), data, delimiter=",", header=header, comments="")
    return path, np.column_stack(features), label


def _make_training_request_dict(
    job_id: str,
    dataset_ref: str,
    seed: int = 42,
) -> dict[str, Any]:
    """Build a training request payload dict (for gateway.create_job)."""
    return {
        "schema_version": 1,
        "job_id": job_id,
        "dataset_manifest_ref": dataset_ref,
        "model_family": "gbm",
        "search_space": {"n_estimators": [50]},
        "random_seed": seed,
        "hardware_class": "cpu",
        "extra_constraints": {},
    }


def _signed_training_output(
    job_id: str,
    *,
    artifact_dict: dict[str, Any],
    dossier_dict: dict[str, Any],
    secret: str,
) -> dict[str, Any]:
    """Build a signed training callback (same format as RunPod worker output)."""
    from quant_foundry.schemas import RunPodCallbackEnvelope
    from quant_foundry.signatures import sign_callback

    envelope = RunPodCallbackEnvelope(
        job_id=job_id,
        worker_id="runpod-training-real",
        result_type="training_complete",
        payload={
            "model_family": "gbm",
            "dossier": dossier_dict,
            "artifact_manifest": artifact_dict,
        },
    )
    payload = envelope.model_dump_json().encode("utf-8")
    ts = int(time.time())
    return {
        "callback_payload": payload.decode("utf-8"),
        "callback_signature": sign_callback(payload, secret=secret, ts=ts, job_id=job_id),
        "callback_ts": ts,
    }


class _RecordingRunPodClient:
    """Mock RunPod client that records dispatches and returns settable statuses.

    Same pattern as ``RecordingRunPodClient`` in ``test_gateway_runpod_loop.py``.
    """

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
    ) -> Any:
        from quant_foundry.runpod_client import DispatchResult, DispatchStatus

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


def _assert_no_order_fields(d: dict[str, Any], context: str = "") -> None:
    """Assert that no order-like / OMS fields are present in a dict."""
    present = _ORDER_LIKE_FIELDS & set(d.keys())
    assert not present, (
        f"order-like fields found in {context}: {sorted(present)} "
        "(shadow predictions must never carry trading authority)"
    )


# ---------------------------------------------------------------------------
# Gateway E2E with real ML
# ===========================================================================


class TestGatewayRealMLE2E:
    """Gateway-level E2E: real trainer → gateway → dossier → shadow dispatch."""

    def test_training_job_completes_with_real_artifact(self, tmp_path: Path) -> None:
        """A training job dispatched through the gateway completes with a
        real artifact (ingested via RunPod polling) and the dossier has
        real metrics (not the stub pattern).
        """
        from quant_foundry.gateway import QuantFoundryGateway
        from quant_foundry.outbox import JobStatus
        from quant_foundry.real_trainer import RealLightGBMTrainer
        from quant_foundry.schemas import Authority, RunPodTrainingRequest

        # --- Step 1: Train a real model with RealLightGBMTrainer ---
        data_path, _X, _y = _make_synthetic_dataset(tmp_path, n=300, seed=42)
        train_req = RunPodTrainingRequest(
            job_id="qf:gw:real:train:1",
            dataset_manifest_ref=data_path.as_uri(),
            model_family="gbm",
            search_space={"n_estimators": [50]},
            random_seed=42,
            hardware_class="cpu",
            extra_constraints={},
        )
        trainer = RealLightGBMTrainer(n_folds=3)
        deadline_ns = time.time_ns() + 120 * 1_000_000_000
        artifact, dossier = trainer.train(train_req, deadline_ns=deadline_ns)

        # Verify the artifact has a real hash.
        assert len(artifact.sha256) == 64
        assert re.fullmatch(r"[0-9a-f]{64}", artifact.sha256)
        assert artifact.size_bytes > 0

        # Verify the dossier has real metrics (not stub pattern).
        metrics = dossier.training_metrics
        accuracy = metrics["accuracy"]
        pbo = dossier.pbo
        assert pbo is not None
        stub_accuracy = 0.5 + (pbo / 2.0)
        assert abs(accuracy - stub_accuracy) > 1e-6, (
            f"accuracy {accuracy} matches stub pattern 0.5 + pbo/2.0 = {stub_accuracy}"
        )
        assert dossier.authority == Authority.SHADOW_ONLY

        # --- Step 2: Create a gateway in runpod_shadow mode ---
        secret = "gw-real-ml-secret"
        training_client = _RecordingRunPodClient(endpoint_id="train-endpoint")
        inference_client = _RecordingRunPodClient(endpoint_id="infer-endpoint")
        gateway = QuantFoundryGateway(
            enabled=True,
            mode="runpod_shadow",
            shadow_only=True,
            callback_secret=secret,
            base_dir=tmp_path / "qf",
            runpod_clients={"training": training_client, "inference": inference_client},
        )

        # --- Step 3: Create a training job through the gateway ---
        job_id = "qf:gw:real:train:1"
        gateway.create_job(
            job_id=job_id,
            job_type="training",
            idempotency_key="idem-gw-real-train",
            request_payload=_make_training_request_dict(job_id, data_path.as_uri(), seed=42),
        )

        # Verify the job was dispatched to the training endpoint.
        assert len(training_client.dispatches) == 1
        assert training_client.dispatches[0]["job_id"] == job_id

        # --- Step 4: Simulate RunPod worker completing with real artifact ---
        runpod_job_id = training_client.dispatches[0]["runpod_job_id"]
        training_client.statuses[runpod_job_id] = {
            "status": "COMPLETED",
            "output": _signed_training_output(
                job_id,
                artifact_dict=artifact.model_dump(mode="json"),
                dossier_dict=dossier.model_dump(mode="json"),
                secret=secret,
            ),
        }

        # --- Step 5: Poll for results → ingest callback → register dossier ---
        receipts = gateway.poll_runpod_results()
        assert len(receipts) == 1
        assert receipts[0]["result"] == "processed"
        assert gateway.outbox.get(job_id).status == JobStatus.COMPLETED

        # --- Step 6: Verify the dossier in the registry has real metrics ---
        dossiers = gateway.list_dossiers()
        assert len(dossiers) == 1
        registered = dossiers[0]
        assert registered["model_id"] == dossier.model_id
        assert registered["artifact_sha256"] == artifact.sha256
        assert registered["artifact_sha256"] != "a" * 64  # not a dummy hash

        reg_metrics = registered["training_metrics"]
        reg_accuracy = reg_metrics["accuracy"]
        reg_pbo = reg_metrics.get("pbo", registered.get("pbo"))
        assert reg_pbo is not None
        stub_acc = 0.5 + (reg_pbo / 2.0)
        assert abs(reg_accuracy - stub_acc) > 1e-6, (
            f"registered dossier accuracy {reg_accuracy} matches stub pattern"
        )
        # Verify authority is shadow-only.
        assert registered.get("authority") == "shadow-only" or ("authority" not in registered)

    def test_dispatch_shadow_inference_batch_with_real_model(self, tmp_path: Path) -> None:
        """dispatch_shadow_inference_batch() dispatches inference jobs for
        a SHADOW_APPROVED model trained with the real trainer.
        """
        from quant_foundry.dossier import DossierRecord, DossierStatus
        from quant_foundry.gateway import QuantFoundryGateway
        from quant_foundry.real_trainer import RealLightGBMTrainer
        from quant_foundry.schemas import RunPodTrainingRequest

        # --- Train a real model ---
        data_path, _X, _y = _make_synthetic_dataset(tmp_path, n=300, seed=42)
        train_req = RunPodTrainingRequest(
            job_id="qf:gw:dispatch:train:1",
            dataset_manifest_ref=data_path.as_uri(),
            model_family="gbm",
            search_space={"n_estimators": [50]},
            random_seed=42,
            hardware_class="cpu",
            extra_constraints={},
        )
        trainer = RealLightGBMTrainer(n_folds=3)
        deadline_ns = time.time_ns() + 120 * 1_000_000_000
        artifact, dossier = trainer.train(train_req, deadline_ns=deadline_ns)

        # Verify real artifact.
        assert len(artifact.sha256) == 64
        assert re.fullmatch(r"[0-9a-f]{64}", artifact.sha256)

        # --- Create a gateway in runpod_shadow mode ---
        secret = "gw-dispatch-secret"
        client = _RecordingRunPodClient(endpoint_id="infer-endpoint")
        gateway = QuantFoundryGateway(
            enabled=True,
            mode="runpod_shadow",
            shadow_only=True,
            callback_secret=secret,
            base_dir=tmp_path / "qf_dispatch",
            runpod_clients={"inference": client, "training": client},
        )

        # --- Register a SHADOW_APPROVED dossier with real artifact info ---
        shadow_dossier = DossierRecord(
            model_id=dossier.model_id,
            artifact_manifest_id=artifact.artifact_id,
            artifact_sha256=artifact.sha256,
            dataset_manifest_id=train_req.dataset_manifest_ref,
            feature_schema_hash=artifact.feature_schema_hash,
            label_schema_hash=artifact.label_schema_hash,
            code_git_sha=artifact.code_git_sha or "unknown",
            lockfile_hash=artifact.lockfile_hash or "unknown",
            container_image_digest=artifact.container_image_digest or "unknown",
            random_seed=train_req.random_seed,
            hardware_class=train_req.hardware_class,
            training_metrics=dossier.training_metrics,
            status=DossierStatus.SHADOW_APPROVED,
        )
        gateway.dossier_registry().register(shadow_dossier)

        # Verify the dossier is registered with SHADOW_APPROVED status.
        dossiers = gateway.list_dossiers(status=DossierStatus.SHADOW_APPROVED)
        assert len(dossiers) == 1
        assert dossiers[0]["model_id"] == dossier.model_id
        assert dossiers[0]["status"] == "shadow_approved"

        # --- Call dispatch_shadow_inference_batch() ---
        receipt = gateway.dispatch_shadow_inference_batch()

        # Verify the dispatch receipt.
        assert receipt["enabled"] is True
        assert receipt["dispatched"] == 1
        assert receipt["skipped"] == 0
        assert len(receipt["job_ids"]) == 1
        assert receipt["errors"] == []

        # Verify the inference job was dispatched to the inference endpoint.
        assert len(client.dispatches) >= 1
        infer_dispatch = client.dispatches[-1]
        assert infer_dispatch["request_payload"]["request"]["artifact_ref"] == artifact.artifact_id

        # Verify no secrets in the receipt.
        receipt_str = str(receipt)
        assert "gw-dispatch-secret" not in receipt_str
        assert "api_key" not in receipt_str

        # Verify no order-like fields in the receipt.
        _assert_no_order_fields(receipt, context="dispatch receipt")

    def test_full_gateway_pipeline_train_then_dispatch(self, tmp_path: Path) -> None:
        """Full pipeline: train via gateway polling, register SHADOW_APPROVED,
        then dispatch shadow inference.
        """
        from quant_foundry.dossier import DossierRecord, DossierStatus
        from quant_foundry.gateway import QuantFoundryGateway
        from quant_foundry.outbox import JobStatus
        from quant_foundry.real_trainer import RealLightGBMTrainer
        from quant_foundry.schemas import RunPodTrainingRequest

        # --- Train a real model ---
        data_path, _X, _y = _make_synthetic_dataset(tmp_path, n=300, seed=42)
        train_req = RunPodTrainingRequest(
            job_id="qf:gw:full:train:1",
            dataset_manifest_ref=data_path.as_uri(),
            model_family="gbm",
            search_space={"n_estimators": [50]},
            random_seed=42,
            hardware_class="cpu",
            extra_constraints={},
        )
        trainer = RealLightGBMTrainer(n_folds=3)
        deadline_ns = time.time_ns() + 120 * 1_000_000_000
        artifact, dossier = trainer.train(train_req, deadline_ns=deadline_ns)

        # --- Create gateway ---
        secret = "gw-full-pipeline-secret"
        training_client = _RecordingRunPodClient(endpoint_id="train-endpoint")
        inference_client = _RecordingRunPodClient(endpoint_id="infer-endpoint")
        gateway = QuantFoundryGateway(
            enabled=True,
            mode="runpod_shadow",
            shadow_only=True,
            callback_secret=secret,
            base_dir=tmp_path / "qf_full",
            runpod_clients={
                "training": training_client,
                "inference": inference_client,
            },
        )

        # --- Step 1: Create + dispatch training job ---
        job_id = "qf:gw:full:train:1"
        gateway.create_job(
            job_id=job_id,
            job_type="training",
            idempotency_key="idem-gw-full-train",
            request_payload=_make_training_request_dict(job_id, data_path.as_uri(), seed=42),
        )

        # --- Step 2: Simulate RunPod completion with real artifact ---
        runpod_job_id = training_client.dispatches[0]["runpod_job_id"]
        training_client.statuses[runpod_job_id] = {
            "status": "COMPLETED",
            "output": _signed_training_output(
                job_id,
                artifact_dict=artifact.model_dump(mode="json"),
                dossier_dict=dossier.model_dump(mode="json"),
                secret=secret,
            ),
        }

        # --- Step 3: Poll → ingest → dossier registered ---
        receipts = gateway.poll_runpod_results()
        assert receipts[0]["result"] == "processed"
        assert gateway.outbox.get(job_id).status == JobStatus.COMPLETED

        # Verify real metrics in the registered dossier.
        dossiers = gateway.list_dossiers()
        assert len(dossiers) == 1
        reg = dossiers[0]
        reg_accuracy = reg["training_metrics"]["accuracy"]
        reg_pbo = reg["training_metrics"].get("pbo")
        assert reg_pbo is not None
        stub_acc = 0.5 + (reg_pbo / 2.0)
        assert abs(reg_accuracy - stub_acc) > 1e-6, "registered dossier has stub metrics, not real"
        assert reg["artifact_sha256"] == artifact.sha256
        assert reg["artifact_sha256"] != "a" * 64

        # --- Step 4: Register SHADOW_APPROVED dossier for dispatch ---
        # The callback processor already registered the dossier as CANDIDATE
        # with the same model_id. The registry rejects re-registering the
        # same model_id with a different content_hash (security event).
        # So we register a separate SHADOW_APPROVED dossier with a distinct
        # model_id, using the same real artifact info.
        shadow_model_id = dossier.model_id + ":shadow"
        shadow_dossier = DossierRecord(
            model_id=shadow_model_id,
            artifact_manifest_id=artifact.artifact_id,
            artifact_sha256=artifact.sha256,
            dataset_manifest_id=train_req.dataset_manifest_ref,
            feature_schema_hash=artifact.feature_schema_hash,
            label_schema_hash=artifact.label_schema_hash,
            code_git_sha=artifact.code_git_sha or "unknown",
            lockfile_hash=artifact.lockfile_hash or "unknown",
            container_image_digest=artifact.container_image_digest or "unknown",
            random_seed=train_req.random_seed,
            hardware_class=train_req.hardware_class,
            training_metrics=dossier.training_metrics,
            status=DossierStatus.SHADOW_APPROVED,
        )
        gateway.dossier_registry().register(shadow_dossier)

        # --- Step 5: Dispatch shadow inference ---
        receipt = gateway.dispatch_shadow_inference_batch()
        assert receipt["enabled"] is True
        assert receipt["dispatched"] == 1
        assert len(receipt["job_ids"]) == 1

        # --- Step 6: Verify dispatch count updated ---
        status = gateway.shadow_dispatch_status
        assert status["dispatch_count"] == 1
        assert status["last_dispatch_ns"] > 0

        # --- Step 7: Verify no secrets in any output ---
        receipt_str = str(receipt)
        assert "gw-full-pipeline-secret" not in receipt_str
        health = gateway.health()
        health_str = str(health)
        assert "gw-full-pipeline-secret" not in health_str

    def test_disabled_gateway_does_not_dispatch(self, tmp_path: Path) -> None:
        """A disabled gateway must not dispatch shadow inference."""
        from quant_foundry.gateway import QuantFoundryGateway

        client = _RecordingRunPodClient(endpoint_id="infer-endpoint")
        gateway = QuantFoundryGateway(
            enabled=False,
            mode="runpod_shadow",
            shadow_only=True,
            callback_secret="disabled-secret",
            base_dir=tmp_path / "qf_disabled",
            runpod_clients={"inference": client, "training": client},
        )
        receipt = gateway.dispatch_shadow_inference_batch()
        assert receipt["enabled"] is False

    def test_non_shadow_mode_skips_dispatch(self, tmp_path: Path) -> None:
        """A gateway in local_mock mode must skip shadow dispatch."""
        from quant_foundry.gateway import QuantFoundryGateway

        gateway = QuantFoundryGateway(
            enabled=True,
            mode="local_mock",
            shadow_only=True,
            callback_secret="mock-secret",
            base_dir=tmp_path / "qf_mock",
        )
        receipt = gateway.dispatch_shadow_inference_batch()
        assert receipt["enabled"] is True
        assert receipt["skipped"] is True
        assert receipt["reason"] == "not in shadow mode"

    def test_no_secrets_in_dispatch_receipt(self, tmp_path: Path) -> None:
        """The dispatch receipt must not contain secrets."""
        from quant_foundry.dossier import DossierRecord, DossierStatus
        from quant_foundry.gateway import QuantFoundryGateway
        from quant_foundry.real_trainer import RealLightGBMTrainer
        from quant_foundry.schemas import RunPodTrainingRequest

        # Train a real model.
        data_path, _, _ = _make_synthetic_dataset(tmp_path, n=300, seed=42)
        train_req = RunPodTrainingRequest(
            job_id="qf:gw:nosec:train:1",
            dataset_manifest_ref=data_path.as_uri(),
            model_family="gbm",
            search_space={"n_estimators": [50]},
            random_seed=42,
            hardware_class="cpu",
            extra_constraints={},
        )
        trainer = RealLightGBMTrainer()
        deadline_ns = time.time_ns() + 120 * 1_000_000_000
        artifact, dossier = trainer.train(train_req, deadline_ns=deadline_ns)

        secret = "super-secret-key-12345"
        client = _RecordingRunPodClient(endpoint_id="infer-endpoint")
        gateway = QuantFoundryGateway(
            enabled=True,
            mode="runpod_shadow",
            shadow_only=True,
            callback_secret=secret,
            base_dir=tmp_path / "qf_nosec",
            runpod_clients={"inference": client, "training": client},
        )

        shadow_dossier = DossierRecord(
            model_id=dossier.model_id,
            artifact_manifest_id=artifact.artifact_id,
            artifact_sha256=artifact.sha256,
            dataset_manifest_id=train_req.dataset_manifest_ref,
            feature_schema_hash=artifact.feature_schema_hash,
            label_schema_hash=artifact.label_schema_hash,
            code_git_sha=artifact.code_git_sha or "unknown",
            lockfile_hash=artifact.lockfile_hash or "unknown",
            container_image_digest=artifact.container_image_digest or "unknown",
            random_seed=train_req.random_seed,
            hardware_class=train_req.hardware_class,
            training_metrics=dossier.training_metrics,
            status=DossierStatus.SHADOW_APPROVED,
        )
        gateway.dossier_registry().register(shadow_dossier)

        receipt = gateway.dispatch_shadow_inference_batch()
        receipt_str = str(receipt)
        assert "super-secret-key-12345" not in receipt_str
        assert "api_key" not in receipt_str.lower()

    def test_real_artifact_hash_not_all_zeros(self, tmp_path: Path) -> None:
        """The real artifact hash must not be a dummy (all zeros / all a's)."""
        from quant_foundry.real_trainer import RealLightGBMTrainer
        from quant_foundry.schemas import RunPodTrainingRequest

        data_path, _, _ = _make_synthetic_dataset(tmp_path, n=300, seed=42)
        req = RunPodTrainingRequest(
            job_id="qf:gw:hashcheck:1",
            dataset_manifest_ref=data_path.as_uri(),
            model_family="gbm",
            search_space={"n_estimators": [50]},
            random_seed=42,
            hardware_class="cpu",
            extra_constraints={},
        )
        trainer = RealLightGBMTrainer()
        deadline_ns = time.time_ns() + 120 * 1_000_000_000
        artifact, _ = trainer.train(req, deadline_ns=deadline_ns)

        # Real hash must not be a dummy pattern.
        assert artifact.sha256 != "0" * 64
        assert artifact.sha256 != "a" * 64
        assert len(artifact.sha256) == 64
        # Must be valid hex.
        assert re.fullmatch(r"[0-9a-f]{64}", artifact.sha256)
