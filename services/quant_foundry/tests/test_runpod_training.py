"""
TDD tests for quant_foundry.runpod_training (TASK-0501).

Tests the RunPod training handler contract locally (no GPU, no Docker, no
broker credentials). The handler uses the SAME schemas, signatures, and
callback envelope as the mock dispatcher (TASK-0305), proving the contract
is identical — flipping to RunPod is a dispatcher-only change.

Acceptance (from NEXT_STEPS_PLAN TASK-0501):
- Local mock trainer and container handler use the same contract.
- No broker credentials are available.
- Artifact manifest is hash-verifiable.
- Training failure returns a safe terminal or retryable status.
- Time and budget limits enforced.
"""

from __future__ import annotations

import json

import pytest
from quant_foundry.runpod_training import (
    LocalTrainer,
    RunPodTrainingHandler,
    TrainingFailure,
)
from quant_foundry.schemas import (
    ArtifactManifest,
    Authority,
    ModelDossier,
    RunPodCallbackEnvelope,
    RunPodTrainingRequest,
)
from quant_foundry.signatures import verify_callback

# --- module imports --------------------------------------------------------


def test_runpod_training_imports() -> None:
    assert callable(RunPodTrainingHandler)
    assert callable(LocalTrainer)
    assert callable(TrainingFailure)


def test_handler_has_no_broker_credentials() -> None:
    """Hard invariant: the handler MUST NOT have any broker/Redis/stream
    attributes. It runs in an isolated container with no trading access."""
    handler = RunPodTrainingHandler(callback_secret="s")
    for attr in ("redis", "broker", "bus", "producer", "stream",
                 "sig_predict_writer", "order_writer", "trading_stream",
                 "FINCEPT_JWT_SECRET", "ALPACA_API_KEY"):
        assert not hasattr(handler, attr), f"handler must not have {attr}"


# --- happy path: training produces signed callback -------------------------


def _make_training_request(job_id: str, seed: int = 42) -> RunPodTrainingRequest:
    return RunPodTrainingRequest(
        job_id=job_id,
        dataset_manifest_ref="ds-manifest-1",
        model_family="gbm",
        search_space={"n_estimators": [100, 200]},
        random_seed=seed,
        hardware_class="mock-gpu",
        extra_constraints={},
    )


def test_handler_trains_and_returns_signed_callback() -> None:
    secret = "test-cb-secret"
    handler = RunPodTrainingHandler(callback_secret=secret)
    job_id = "qf:train:rp:gbm:h1:1"
    req = _make_training_request(job_id, seed=42)

    result = handler.handle(req)

    # Callback envelope validates against the real schema.
    envelope = RunPodCallbackEnvelope.model_validate(json.loads(result.callback_payload))
    assert envelope.job_id == job_id
    assert envelope.result_type == "training_complete"
    assert envelope.worker_id  # non-empty

    # Payload contains a validated dossier + artifact manifest.
    dossier = ModelDossier.model_validate(envelope.payload["dossier"])
    artifact = ArtifactManifest.model_validate(envelope.payload["artifact_manifest"])
    assert dossier.metadata.get("model_family") == "gbm" or "gbm" in dossier.model_id
    assert artifact.sha256  # non-empty
    assert artifact.model_family == "gbm"

    # Signature verifies.
    assert verify_callback(
        result.callback_payload, result.callback_signature,
        secret=secret, ts=result.callback_ts, job_id=job_id,
    )

    # Authority is shadow-only (hard invariant).
    assert dossier.authority == Authority.SHADOW_ONLY


# --- hash verifiability ----------------------------------------------------


def test_artifact_manifest_hash_verifiable_same_inputs() -> None:
    """Same inputs -> same artifact_id (deterministic)."""
    handler = RunPodTrainingHandler(callback_secret="s")
    req = _make_training_request("qf:train:hash:1", seed=42)
    r1 = handler.handle(req)
    r2 = handler.handle(req)
    e1 = RunPodCallbackEnvelope.model_validate(json.loads(r1.callback_payload))
    e2 = RunPodCallbackEnvelope.model_validate(json.loads(r2.callback_payload))
    a1 = ArtifactManifest.model_validate(e1.payload["artifact_manifest"])
    a2 = ArtifactManifest.model_validate(e2.payload["artifact_manifest"])
    assert a1.artifact_id == a2.artifact_id
    assert a1.sha256 == a2.sha256


def test_artifact_manifest_changes_with_different_seed() -> None:
    """Different seed -> different artifact (not silently identical)."""
    handler = RunPodTrainingHandler(callback_secret="s")
    r1 = handler.handle(_make_training_request("qf:train:diff:1", seed=42))
    r2 = handler.handle(_make_training_request("qf:train:diff:1", seed=999))
    e1 = RunPodCallbackEnvelope.model_validate(json.loads(r1.callback_payload))
    e2 = RunPodCallbackEnvelope.model_validate(json.loads(r2.callback_payload))
    a1 = ArtifactManifest.model_validate(e1.payload["artifact_manifest"])
    a2 = ArtifactManifest.model_validate(e2.payload["artifact_manifest"])
    assert a1.artifact_id != a2.artifact_id


# --- training failure ------------------------------------------------------


def test_training_failure_returns_safe_terminal_status() -> None:
    """A training failure must return a safe terminal status, not crash."""
    # Inject a failure by using a handler with a broken trainer.
    handler_fail = RunPodTrainingHandler(
        callback_secret="s",
        trainer=LocalTrainer(should_fail=True),
    )
    req = _make_training_request("qf:train:fail:1")
    with pytest.raises(TrainingFailure) as exc_info:
        handler_fail.handle(req)
    assert exc_info.value.error_code  # non-empty
    assert exc_info.value.error_summary  # non-empty


# --- time/budget limit enforcement -----------------------------------------


def test_time_limit_enforced() -> None:
    """A handler with a 0-second deadline must fail immediately."""
    handler = RunPodTrainingHandler(
        callback_secret="s",
        deadline_seconds=0,
    )
    req = _make_training_request("qf:train:timeout:1")
    with pytest.raises(TrainingFailure, match=r"timeout|deadline|time"):
        handler.handle(req)


# --- same contract as mock dispatcher --------------------------------------


def test_callback_envelope_same_schema_as_mock_dispatcher() -> None:
    """The RunPod handler produces the same RunPodCallbackEnvelope shape
    as the mock dispatcher — proving the contract is identical."""
    secret = "same-contract-secret"
    handler = RunPodTrainingHandler(callback_secret=secret)
    req = _make_training_request("qf:train:same:1", seed=42)
    result = handler.handle(req)

    # The envelope must validate against the same schema used by the mock.
    envelope = RunPodCallbackEnvelope.model_validate(json.loads(result.callback_payload))
    assert envelope.schema_version == 1
    assert envelope.result_type == "training_complete"
    assert "dossier" in envelope.payload
    assert "artifact_manifest" in envelope.payload

    # The signature must verify with the same sign_callback/verify_callback.
    assert verify_callback(
        result.callback_payload, result.callback_signature,
        secret=secret, ts=result.callback_ts, job_id="qf:train:same:1",
    )
