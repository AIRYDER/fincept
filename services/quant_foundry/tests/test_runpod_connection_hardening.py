"""Tests for the Railway ↔ RunPod connection hardening.

Covers:
- Canonical RUNPOD_* env vars load correctly via from_env().
- Deprecated QUANT_FOUNDRY_RUNPOD_* env vars load as fallbacks with a warning.
- Canonical vars override deprecated vars when both are present.
- Fail-closed: from_env() raises RunPodConfigError when runpod mode is
  enabled but required env vars are missing.
- health() reports runpod_config_valid + missing_env without exposing secrets.
- env_first() helper: canonical preference, fallback, default.
- RunPod canary: dispatches a canary job, polls, verifies the signature.
- Polled RunPod output with valid HMAC registers the callback.
- Polled RunPod output with bad/missing signature fails closed.
"""

from __future__ import annotations

import json
import time
import warnings
from typing import Any

import pytest
from quant_foundry.gateway import QuantFoundryGateway, RunPodConfigError
from quant_foundry.gateway_helpers import env_first
from quant_foundry.runpod_client import DispatchResult, DispatchStatus
from quant_foundry.signatures import sign_callback

# Deprecated env var fallback tests emit DeprecationWarning by design.
pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

# --- helpers ----------------------------------------------------------------


def _clear_runpod_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove all RunPod + Quant Foundry env vars so tests start clean."""
    for key in [
        "QUANT_FOUNDRY_ENABLED",
        "QUANT_FOUNDRY_MODE",
        "QUANT_FOUNDRY_SHADOW_ONLY",
        "QUANT_FOUNDRY_CALLBACK_SECRET",
        "QUANT_FOUNDRY_BASE_DIR",
        "QUANT_FOUNDRY_ALLOW_PAPER_BRIDGE",
        "QUANT_FOUNDRY_MONTHLY_BUDGET_CENTS",
        "RUNPOD_API_KEY",
        "RUNPOD_TRAINING_ENDPOINT_ID",
        "RUNPOD_INFERENCE_ENDPOINT_ID",
        "RUNPOD_ENDPOINT_ID",
        "RUNPOD_BASE_URL",
        "RUNPOD_TIMEOUT_SECONDS",
        "RUNPOD_COST_PER_DISPATCH_CENTS",
        "QUANT_FOUNDRY_RUNPOD_API_KEY",
        "QUANT_FOUNDRY_RUNPOD_TRAINING_ENDPOINT",
        "QUANT_FOUNDRY_RUNPOD_INFERENCE_ENDPOINT",
    ]:
        monkeypatch.delenv(key, raising=False)


class CanaryRecordingClient:
    """Mock RunPod client that handles canary jobs with a configurable secret.

    Simulates a RunPod serverless endpoint: dispatch returns a job ID,
    and check_status returns COMPLETED with the canary callback fields
    signed with the worker's copy of the callback secret.
    """

    cost_per_dispatch_cents = 0

    def __init__(self, *, endpoint_id: str, worker_secret: str) -> None:
        self.endpoint_id = endpoint_id
        self._worker_secret = worker_secret
        self.dispatches: list[dict[str, Any]] = []
        self._canary_outputs: dict[str, dict[str, Any]] = {}

    def dispatch(
        self,
        *,
        job_id: str,
        request_payload: dict[str, Any],
        budget_cents: int | None,
    ) -> DispatchResult:
        self.dispatches.append({"job_id": job_id, "request_payload": request_payload})
        runpod_job_id = f"rp-{self.endpoint_id}-{len(self.dispatches)}"

        # If this is a canary job, pre-build the completed output.
        if request_payload.get("task") == "callback_secret_canary":
            nonce = request_payload.get("nonce", "")
            callback_payload = json.dumps(
                {
                    "schema_version": 1,
                    "job_id": job_id,
                    "worker_id": "runpod-canary",
                    "result_type": "callback_secret_canary",
                    "payload": {"nonce": nonce},
                },
                sort_keys=True,
            ).encode("utf-8")
            callback_ts = int(time.time())
            callback_signature = sign_callback(
                callback_payload,
                secret=self._worker_secret,
                ts=callback_ts,
                job_id=job_id,
            )
            self._canary_outputs[runpod_job_id] = {
                "status": "COMPLETED",
                "output": {
                    "callback_payload": callback_payload.decode("utf-8"),
                    "callback_signature": callback_signature,
                    "callback_ts": callback_ts,
                    "canary": True,
                    "nonce": nonce,
                },
            }

        return DispatchResult(
            job_id=job_id,
            status=DispatchStatus.DISPATCHED,
            runpod_job_id=runpod_job_id,
        )

    def check_status(self, runpod_job_id: str) -> dict[str, Any]:
        if runpod_job_id in self._canary_outputs:
            return self._canary_outputs[runpod_job_id]
        return {"status": "IN_PROGRESS"}

    def check_health(self) -> dict[str, Any]:
        return {"endpoint_id": self.endpoint_id, "status": "ok"}


# --- env_first helper tests -------------------------------------------------


def test_env_first_prefers_primary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRIMARY_VAR", "primary-value")
    monkeypatch.setenv("FALLBACK_VAR", "fallback-value")
    assert env_first("PRIMARY_VAR", "FALLBACK_VAR") == "primary-value"


def test_env_first_falls_back_with_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRIMARY_VAR", raising=False)
    monkeypatch.setenv("FALLBACK_VAR", "fallback-value")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = env_first("PRIMARY_VAR", "FALLBACK_VAR")
    assert result == "fallback-value"
    assert len(caught) == 1
    assert issubclass(caught[0].category, DeprecationWarning)
    assert "FALLBACK_VAR" in str(caught[0].message)
    assert "PRIMARY_VAR" in str(caught[0].message)


def test_env_first_returns_default_when_neither_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PRIMARY_VAR", raising=False)
    monkeypatch.delenv("FALLBACK_VAR", raising=False)
    assert env_first("PRIMARY_VAR", "FALLBACK_VAR", default="none") == "none"


def test_env_first_empty_string_falls_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty-string env var should NOT count as 'set' — fall through."""
    monkeypatch.setenv("PRIMARY_VAR", "")
    monkeypatch.setenv("FALLBACK_VAR", "fallback-value")
    assert env_first("PRIMARY_VAR", "FALLBACK_VAR") == "fallback-value"


# --- from_env canonical + legacy tests --------------------------------------


def test_from_env_reads_canonical_runpod_vars(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    _clear_runpod_env(monkeypatch)
    monkeypatch.setenv("QUANT_FOUNDRY_ENABLED", "true")
    monkeypatch.setenv("QUANT_FOUNDRY_MODE", "runpod")
    monkeypatch.setenv("QUANT_FOUNDRY_CALLBACK_SECRET", "test-secret")
    monkeypatch.setenv("RUNPOD_API_KEY", "test-api-key")
    monkeypatch.setenv("RUNPOD_TRAINING_ENDPOINT_ID", "train-ep-1")
    monkeypatch.setenv("RUNPOD_INFERENCE_ENDPOINT_ID", "infer-ep-1")

    gw = QuantFoundryGateway.from_env(base_dir=tmp_path / "qf")
    assert gw._is_runpod_mode()
    assert "training" in gw._runpod_clients
    assert "inference" in gw._runpod_clients
    assert gw._runpod_clients["training"].endpoint_id == "train-ep-1"
    assert gw._runpod_clients["inference"].endpoint_id == "infer-ep-1"


def test_from_env_reads_legacy_quant_foundry_runpod_vars_with_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    _clear_runpod_env(monkeypatch)
    monkeypatch.setenv("QUANT_FOUNDRY_ENABLED", "true")
    monkeypatch.setenv("QUANT_FOUNDRY_MODE", "runpod")
    monkeypatch.setenv("QUANT_FOUNDRY_CALLBACK_SECRET", "test-secret")
    monkeypatch.setenv("QUANT_FOUNDRY_RUNPOD_API_KEY", "legacy-api-key")
    monkeypatch.setenv("QUANT_FOUNDRY_RUNPOD_TRAINING_ENDPOINT", "legacy-train-ep")
    monkeypatch.setenv("QUANT_FOUNDRY_RUNPOD_INFERENCE_ENDPOINT", "legacy-infer-ep")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        gw = QuantFoundryGateway.from_env(base_dir=tmp_path / "qf")

    assert gw._runpod_clients["training"].endpoint_id == "legacy-train-ep"
    assert gw._runpod_clients["inference"].endpoint_id == "legacy-infer-ep"
    # At least 3 deprecation warnings (api key, training, inference).
    dep_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert len(dep_warnings) >= 3


def test_from_env_prefers_canonical_over_legacy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    _clear_runpod_env(monkeypatch)
    monkeypatch.setenv("QUANT_FOUNDRY_ENABLED", "true")
    monkeypatch.setenv("QUANT_FOUNDRY_MODE", "runpod")
    monkeypatch.setenv("QUANT_FOUNDRY_CALLBACK_SECRET", "test-secret")
    monkeypatch.setenv("RUNPOD_API_KEY", "canonical-key")
    monkeypatch.setenv("RUNPOD_TRAINING_ENDPOINT_ID", "canonical-train")
    monkeypatch.setenv("RUNPOD_INFERENCE_ENDPOINT_ID", "canonical-infer")
    monkeypatch.setenv("QUANT_FOUNDRY_RUNPOD_API_KEY", "legacy-key")
    monkeypatch.setenv("QUANT_FOUNDRY_RUNPOD_TRAINING_ENDPOINT", "legacy-train")
    monkeypatch.setenv("QUANT_FOUNDRY_RUNPOD_INFERENCE_ENDPOINT", "legacy-infer")

    gw = QuantFoundryGateway.from_env(base_dir=tmp_path / "qf")
    assert gw._runpod_clients["training"].endpoint_id == "canonical-train"
    assert gw._runpod_clients["inference"].endpoint_id == "canonical-infer"


# --- fail-closed tests ------------------------------------------------------


def test_from_env_fails_closed_when_runpod_mode_and_api_key_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    _clear_runpod_env(monkeypatch)
    monkeypatch.setenv("QUANT_FOUNDRY_ENABLED", "true")
    monkeypatch.setenv("QUANT_FOUNDRY_MODE", "runpod")
    monkeypatch.setenv("QUANT_FOUNDRY_CALLBACK_SECRET", "test-secret")
    monkeypatch.setenv("RUNPOD_TRAINING_ENDPOINT_ID", "train-ep")
    monkeypatch.setenv("RUNPOD_INFERENCE_ENDPOINT_ID", "infer-ep")
    # RUNPOD_API_KEY is NOT set.

    with pytest.raises(RunPodConfigError, match="RUNPOD_API_KEY"):
        QuantFoundryGateway.from_env(base_dir=tmp_path / "qf")


def test_from_env_fails_closed_when_runpod_mode_and_endpoint_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    _clear_runpod_env(monkeypatch)
    monkeypatch.setenv("QUANT_FOUNDRY_ENABLED", "true")
    monkeypatch.setenv("QUANT_FOUNDRY_MODE", "runpod")
    monkeypatch.setenv("QUANT_FOUNDRY_CALLBACK_SECRET", "test-secret")
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    monkeypatch.setenv("RUNPOD_TRAINING_ENDPOINT_ID", "train-ep")
    # RUNPOD_INFERENCE_ENDPOINT_ID is NOT set.

    with pytest.raises(RunPodConfigError, match="RUNPOD_INFERENCE_ENDPOINT_ID"):
        QuantFoundryGateway.from_env(base_dir=tmp_path / "qf")


def test_from_env_fails_closed_when_runpod_mode_and_callback_secret_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    _clear_runpod_env(monkeypatch)
    monkeypatch.setenv("QUANT_FOUNDRY_ENABLED", "true")
    monkeypatch.setenv("QUANT_FOUNDRY_MODE", "runpod")
    monkeypatch.setenv("RUNPOD_API_KEY", "test-key")
    monkeypatch.setenv("RUNPOD_TRAINING_ENDPOINT_ID", "train-ep")
    monkeypatch.setenv("RUNPOD_INFERENCE_ENDPOINT_ID", "infer-ep")
    # QUANT_FOUNDRY_CALLBACK_SECRET is NOT set.

    with pytest.raises(RunPodConfigError, match="QUANT_FOUNDRY_CALLBACK_SECRET"):
        QuantFoundryGateway.from_env(base_dir=tmp_path / "qf")


def test_from_env_does_not_raise_in_local_mock_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """local_mock mode should NOT require RunPod env vars."""
    _clear_runpod_env(monkeypatch)
    monkeypatch.setenv("QUANT_FOUNDRY_ENABLED", "true")
    monkeypatch.setenv("QUANT_FOUNDRY_MODE", "local_mock")
    monkeypatch.setenv("QUANT_FOUNDRY_CALLBACK_SECRET", "test-secret")

    gw = QuantFoundryGateway.from_env(base_dir=tmp_path / "qf")
    assert gw.enabled
    assert gw.mode == "local_mock"
    assert not gw._is_runpod_mode()


# --- health tests -----------------------------------------------------------


def test_health_reports_runpod_config_valid_when_wired(tmp_path: Any) -> None:
    secret = "test-secret"
    gw = QuantFoundryGateway(
        enabled=True,
        mode="runpod",
        shadow_only=True,
        callback_secret=secret,
        base_dir=tmp_path / "qf",
        runpod_clients={
            "training": CanaryRecordingClient(endpoint_id="train-ep", worker_secret=secret),
            "inference": CanaryRecordingClient(endpoint_id="infer-ep", worker_secret=secret),
        },
    )
    h = gw.health()
    assert h["runpod_config_valid"] is True
    assert h["missing_env"] == []
    assert h["runpod_wired"] is True


def test_health_reports_runpod_config_invalid_when_endpoints_missing(
    tmp_path: Any,
) -> None:
    """A runpod-mode gateway with only one client wired should report invalid."""
    secret = "test-secret"
    gw = QuantFoundryGateway(
        enabled=True,
        mode="runpod",
        shadow_only=True,
        callback_secret=secret,
        base_dir=tmp_path / "qf",
        runpod_clients={
            "training": CanaryRecordingClient(endpoint_id="train-ep", worker_secret=secret),
            # inference client missing
        },
    )
    h = gw.health()
    assert h["runpod_config_valid"] is False
    assert "RUNPOD_INFERENCE_ENDPOINT_ID" in h["missing_env"]


def test_health_reports_valid_in_local_mock_mode(tmp_path: Any) -> None:
    gw = QuantFoundryGateway(
        enabled=True,
        mode="local_mock",
        shadow_only=True,
        callback_secret="test-secret",
        base_dir=tmp_path / "qf",
    )
    h = gw.health()
    assert h["runpod_config_valid"] is True
    assert h["missing_env"] == []


def test_health_never_exposes_secrets(tmp_path: Any) -> None:
    secret = "super-secret-value-never-leak"
    gw = QuantFoundryGateway(
        enabled=True,
        mode="runpod",
        shadow_only=True,
        callback_secret=secret,
        base_dir=tmp_path / "qf",
        runpod_clients={
            "training": CanaryRecordingClient(endpoint_id="train-ep", worker_secret=secret),
            "inference": CanaryRecordingClient(endpoint_id="infer-ep", worker_secret=secret),
        },
    )
    h = gw.health()
    health_json = json.dumps(h)
    assert secret not in health_json


# --- canary tests -----------------------------------------------------------


def test_runpod_canary_verifies_when_secrets_match(tmp_path: Any) -> None:
    secret = "shared-canary-secret"
    client = CanaryRecordingClient(endpoint_id="train-ep", worker_secret=secret)
    gw = QuantFoundryGateway(
        enabled=True,
        mode="runpod",
        shadow_only=True,
        callback_secret=secret,
        base_dir=tmp_path / "qf",
        runpod_clients={
            "training": client,
            "inference": CanaryRecordingClient(endpoint_id="infer-ep", worker_secret=secret),
        },
    )
    receipt = gw.runpod_canary(job_type="training")
    assert receipt["ok"] is True
    assert receipt["verified"] is True
    assert receipt["job_type"] == "training"
    assert receipt["nonce"]
    assert receipt["detail"] == "signature verified"
    # The canary dispatched exactly one job.
    assert len(client.dispatches) == 1
    assert client.dispatches[0]["request_payload"]["task"] == "callback_secret_canary"


def test_runpod_canary_fails_when_secrets_differ(tmp_path: Any) -> None:
    api_secret = "api-side-secret"
    worker_secret = "worker-side-secret-DIFFERENT"
    client = CanaryRecordingClient(endpoint_id="train-ep", worker_secret=worker_secret)
    gw = QuantFoundryGateway(
        enabled=True,
        mode="runpod",
        shadow_only=True,
        callback_secret=api_secret,
        base_dir=tmp_path / "qf",
        runpod_clients={
            "training": client,
            "inference": CanaryRecordingClient(endpoint_id="infer-ep", worker_secret=worker_secret),
        },
    )
    receipt = gw.runpod_canary(job_type="training")
    assert receipt["ok"] is False
    assert receipt["verified"] is False
    assert "signature verification failed" in receipt["detail"]


def test_runpod_canary_returns_not_runpod_mode_in_local_mock(tmp_path: Any) -> None:
    gw = QuantFoundryGateway(
        enabled=True,
        mode="local_mock",
        shadow_only=True,
        callback_secret="test-secret",
        base_dir=tmp_path / "qf",
    )
    receipt = gw.runpod_canary()
    assert receipt["ok"] is False
    assert receipt["verified"] is False
    assert "not in runpod mode" in receipt["detail"]


def test_runpod_canary_returns_no_client_when_endpoint_not_wired(
    tmp_path: Any,
) -> None:
    secret = "test-secret"
    gw = QuantFoundryGateway(
        enabled=True,
        mode="runpod",
        shadow_only=True,
        callback_secret=secret,
        base_dir=tmp_path / "qf",
        runpod_clients={
            "training": CanaryRecordingClient(endpoint_id="train-ep", worker_secret=secret),
            # inference not wired
        },
    )
    receipt = gw.runpod_canary(job_type="inference")
    assert receipt["ok"] is False
    assert "no RunPod client wired" in receipt["detail"]


# --- polling callback verification tests ------------------------------------


def test_polled_runpod_output_with_valid_hmac_registers_callback(
    tmp_path: Any,
) -> None:
    """A polled RunPod completion with a valid HMAC signature should
    route through receive_callback and register the dossier/prediction."""
    from quant_foundry.schemas import (
        ArtifactManifest,
        Authority,
        ModelDossier,
        RunPodCallbackEnvelope,
    )

    secret = "poll-verify-secret"
    job_id = "qf:train:poll:1"

    # Build a signed training callback output (as RunPod would return).
    artifact = ArtifactManifest(
        artifact_id="artifact:poll",
        sha256="b" * 64,
        size_bytes=2048,
        uri=None,
        model_family="gbm",
        created_at_ns=time.time_ns(),
        feature_schema_hash="fh",
        label_schema_hash="lh",
        code_git_sha="sha",
        lockfile_hash="lh",
        container_image_digest="cd",
    )
    dossier = ModelDossier(
        model_id="model:poll",
        artifact_manifest_id=artifact.artifact_id,
        dataset_manifest_id="dataset:poll",
        code_git_sha="sha",
        lockfile_hash="lh",
        container_image_digest="cd",
        random_seed=42,
        hardware_class="runpod-gpu",
        training_metrics={"accuracy": 0.6},
        pbo=0.2,
        deflated_sharpe=0.5,
        authority=Authority.SHADOW_ONLY,
    )
    envelope = RunPodCallbackEnvelope(
        job_id=job_id,
        worker_id="runpod-training",
        result_type="training_complete",
        payload={
            "model_family": "gbm",
            "dossier": dossier.model_dump(mode="json"),
            "artifact_manifest": artifact.model_dump(mode="json"),
        },
    )
    payload = envelope.model_dump_json().encode("utf-8")
    ts = int(time.time())
    signed_output = {
        "status": "COMPLETED",
        "output": {
            "callback_payload": payload.decode("utf-8"),
            "callback_signature": sign_callback(payload, secret=secret, ts=ts, job_id=job_id),
            "callback_ts": ts,
        },
    }

    # Wire a client that returns the signed output on poll.
    class PollClient:
        cost_per_dispatch_cents = 0
        endpoint_id = "train-ep"

        def __init__(self) -> None:
            self.dispatches: list[dict[str, Any]] = []

        def dispatch(self, **kwargs: Any) -> DispatchResult:
            rj = f"rp-{len(self.dispatches) + 1}"
            self.dispatches.append({"job_id": kwargs["job_id"], "runpod_job_id": rj})
            return DispatchResult(
                job_id=kwargs["job_id"],
                status=DispatchStatus.DISPATCHED,
                runpod_job_id=rj,
            )

        def check_status(self, runpod_job_id: str) -> dict[str, Any]:
            return signed_output

        def check_health(self) -> dict[str, Any]:
            return {"status": "ok"}

    client = PollClient()
    gw = QuantFoundryGateway(
        enabled=True,
        mode="runpod",
        shadow_only=True,
        callback_secret=secret,
        base_dir=tmp_path / "qf",
        runpod_clients={
            "training": client,
            "inference": CanaryRecordingClient(endpoint_id="infer-ep", worker_secret=secret),
        },
    )

    # Create + dispatch a training job.
    gw.create_job(
        job_id=job_id,
        job_type="training",
        idempotency_key="idem-poll",
        request_payload={
            "schema_version": 1,
            "job_id": job_id,
            "dataset_manifest_ref": "dataset:poll",
            "model_family": "gbm",
            "search_space": {"n_estimators": [64]},
            "random_seed": 42,
            "hardware_class": "runpod-gpu",
            "extra_constraints": {},
        },
    )

    # Poll — should verify the signature and register the callback.
    receipts = gw.poll_runpod_results()
    assert len(receipts) == 1
    assert receipts[0]["ok"] is True
    assert receipts[0]["job_id"] == job_id


def test_polled_runpod_output_missing_signature_fails_closed(
    tmp_path: Any,
) -> None:
    """A polled RunPod completion without callback fields should fail closed."""
    secret = "poll-fail-secret"
    job_id = "qf:train:pollfail:1"

    class NoSignatureClient:
        cost_per_dispatch_cents = 0
        endpoint_id = "train-ep"

        def dispatch(self, **kwargs: Any) -> DispatchResult:
            return DispatchResult(
                job_id=kwargs["job_id"],
                status=DispatchStatus.DISPATCHED,
                runpod_job_id="rp-nosig-1",
            )

        def check_status(self, runpod_job_id: str) -> dict[str, Any]:
            return {"status": "COMPLETED", "output": {"job_id": job_id}}

        def check_health(self) -> dict[str, Any]:
            return {"status": "ok"}

    gw = QuantFoundryGateway(
        enabled=True,
        mode="runpod",
        shadow_only=True,
        callback_secret=secret,
        base_dir=tmp_path / "qf",
        runpod_clients={
            "training": NoSignatureClient(),
            "inference": CanaryRecordingClient(endpoint_id="infer-ep", worker_secret=secret),
        },
    )
    gw.create_job(
        job_id=job_id,
        job_type="training",
        idempotency_key="idem-nosig",
        request_payload={
            "schema_version": 1,
            "job_id": job_id,
            "dataset_manifest_ref": "dataset:nosig",
            "model_family": "gbm",
            "search_space": {},
            "random_seed": 1,
            "hardware_class": "gpu",
            "extra_constraints": {},
        },
    )
    receipts = gw.poll_runpod_results()
    assert len(receipts) == 1
    assert receipts[0]["ok"] is False
    assert receipts[0]["error_code"] == "missing_runpod_callback_fields"
