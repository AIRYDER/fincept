"""
Tests for quant_foundry.real_trainer — real LightGBM trainer (TASK-0502).

Tests verify:
- The module is importable without ML deps (lazy imports).
- RealLightGBMTrainer produces a real artifact with non-synthetic hash.
- Training produces real metrics (not the LocalTrainer synthetic pattern).
- Deadline enforcement works.
- Authority.SHADOW_ONLY is always enforced.
- The trainer can be injected into RunPodTrainingHandler.
- LocalTrainer still works (backward compat).

Tests requiring lightgbm use ``pytest.importorskip("lightgbm")`` so they
are skipped in environments without ML deps.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest

# --- lazy import tests (no ML deps required) --------------------------------


def test_real_trainer_module_importable() -> None:
    """The real_trainer module must be importable without lightgbm/numpy."""
    import quant_foundry.real_trainer as rt

    assert callable(rt.RealLightGBMTrainer)


def test_real_trainer_lazy_imports_no_module_level_ml_deps() -> None:
    """lightgbm and numpy must NOT be imported at module level."""
    import quant_foundry.real_trainer as rt

    assert not hasattr(rt, "lightgbm"), "lightgbm must not be a module-level attribute"
    assert not hasattr(rt, "numpy"), "numpy must not be a module-level attribute"
    assert not hasattr(rt, "lgb"), "lgb alias must not be a module-level attribute"
    assert not hasattr(rt, "np"), "np alias must not be a module-level attribute"


def test_real_trainer_class_is_dataclass() -> None:
    """RealLightGBMTrainer should be a dataclass (same style as LocalTrainer)."""
    import dataclasses

    from quant_foundry.real_trainer import RealLightGBMTrainer

    assert dataclasses.is_dataclass(RealLightGBMTrainer)
    trainer = RealLightGBMTrainer()
    assert trainer.should_fail is False
    assert trainer.n_folds == 3


# --- tests requiring lightgbm -----------------------------------------------

_LIGHTGBM = pytest.importorskip("lightgbm")
_NUMPY = pytest.importorskip("numpy")


def _make_test_dataset(tmp_path: Path, n: int = 300, seed: int = 42) -> Path:
    """Create a synthetic CSV dataset with signal for LightGBM training.

    Layout: timestamp, f1, f2, f3, f4 (noise), label (binary).
    The label has real signal from f1/f2/f3 so accuracy > 0.5.
    """
    import numpy as np

    rng = np.random.RandomState(seed)
    timestamps = np.arange(n, dtype=np.int64)
    f1 = rng.randn(n)
    f2 = rng.randn(n)
    f3 = rng.randn(n)
    f4 = rng.randn(n)
    logit = 0.8 * f1 + 0.5 * f2 - 0.6 * f3 + 0.05 * rng.randn(n)
    label = (logit > 0).astype(float)
    data = np.column_stack([timestamps, f1, f2, f3, f4, label])
    path = tmp_path / "test_data.csv"
    np.savetxt(
        str(path),
        data,
        delimiter=",",
        header="timestamp,f1,f2,f3,f4,label",
        comments="",
    )
    return path


def _make_training_request(
    job_id: str,
    dataset_ref: str,
    seed: int = 42,
) -> "RunPodTrainingRequest":
    from quant_foundry.schemas import RunPodTrainingRequest

    return RunPodTrainingRequest(
        job_id=job_id,
        dataset_manifest_ref=dataset_ref,
        model_family="gbm",
        search_space={"n_estimators": [50]},
        random_seed=seed,
        hardware_class="cpu",
        extra_constraints={},
    )


def test_real_trainer_produces_real_artifact(tmp_path: Path) -> None:
    """Training must produce a real artifact with a non-synthetic sha256."""
    from quant_foundry.real_trainer import RealLightGBMTrainer
    from quant_foundry.schemas import ArtifactManifest

    data_path = _make_test_dataset(tmp_path)
    req = _make_training_request(
        "qf:train:real:1",
        data_path.as_uri(),
        seed=42,
    )

    trainer = RealLightGBMTrainer()
    deadline_ns = time.time_ns() + 120 * 1_000_000_000
    artifact, dossier = trainer.train(req, deadline_ns=deadline_ns)

    assert isinstance(artifact, ArtifactManifest)
    assert artifact.sha256  # non-empty
    assert len(artifact.sha256) == 64  # real sha256 hex
    assert re.fullmatch(r"[0-9a-f]{64}", artifact.sha256), "sha256 must be hex"
    assert artifact.size_bytes > 0  # real model bytes
    assert artifact.artifact_id.startswith("artifact:")
    assert artifact.model_family == "gbm"


def test_real_trainer_artifact_hash_not_synthetic(tmp_path: Path) -> None:
    """The real trainer's hash must differ from the LocalTrainer's synthetic hash.

    The LocalTrainer derives its hash from request inputs (a JSON canonical
    blob). The real trainer derives its hash from pickled model bytes. These
    are fundamentally different, so the hashes must not match.
    """
    from quant_foundry.real_trainer import RealLightGBMTrainer
    from quant_foundry.runpod_training import LocalTrainer

    data_path = _make_test_dataset(tmp_path)
    req = _make_training_request(
        "qf:train:hash:1",
        data_path.as_uri(),
        seed=42,
    )

    deadline_ns = time.time_ns() + 120 * 1_000_000_000

    real_trainer = RealLightGBMTrainer()
    real_artifact, _ = real_trainer.train(req, deadline_ns=deadline_ns)

    local_trainer = LocalTrainer()
    local_req = _make_training_request(
        "qf:train:hash:1",
        "ds-manifest-1",
        seed=42,
    )
    local_artifact, _ = local_trainer.train(local_req, deadline_ns=deadline_ns)

    assert real_artifact.sha256 != local_artifact.sha256


def test_real_trainer_produces_real_metrics(tmp_path: Path) -> None:
    """Training metrics must be real, not the LocalTrainer synthetic pattern.

    The LocalTrainer uses ``accuracy = 0.5 + (pbo / 2.0)`` and
    ``logloss = 0.7 - (pbo / 4.0)``. The real trainer computes accuracy from
    actual model predictions, so it must NOT match this formula.
    """
    from quant_foundry.real_trainer import RealLightGBMTrainer

    data_path = _make_test_dataset(tmp_path)
    req = _make_training_request(
        "qf:train:metrics:1",
        data_path.as_uri(),
        seed=42,
    )

    trainer = RealLightGBMTrainer()
    deadline_ns = time.time_ns() + 120 * 1_000_000_000
    artifact, dossier = trainer.train(req, deadline_ns=deadline_ns)

    metrics = dossier.training_metrics
    assert "accuracy" in metrics
    assert "logloss" in metrics
    assert "brier_score" in metrics
    assert "sharpe_ratio" in metrics
    assert "max_drawdown" in metrics
    assert "win_rate" in metrics

    accuracy = metrics["accuracy"]
    pbo = dossier.pbo
    assert pbo is not None

    synthetic_accuracy = 0.5 + (pbo / 2.0)
    assert abs(accuracy - synthetic_accuracy) > 1e-6, (
        f"accuracy {accuracy} matches synthetic pattern 0.5 + pbo/2.0 = {synthetic_accuracy}"
    )

    assert 0.0 <= accuracy <= 1.0
    assert metrics["logloss"] > 0.0
    assert 0.0 <= metrics["brier_score"] <= 1.0
    assert 0.0 <= metrics["win_rate"] <= 1.0
    assert metrics["max_drawdown"] <= 0.0


def test_real_trainer_deterministic_same_seed(tmp_path: Path) -> None:
    """Same seed + data must produce the same artifact hash (deterministic)."""
    from quant_foundry.real_trainer import RealLightGBMTrainer

    data_path = _make_test_dataset(tmp_path)
    req = _make_training_request(
        "qf:train:det:1",
        data_path.as_uri(),
        seed=42,
    )

    trainer = RealLightGBMTrainer()
    deadline_ns = time.time_ns() + 120 * 1_000_000_000

    artifact1, dossier1 = trainer.train(req, deadline_ns=deadline_ns)
    artifact2, dossier2 = trainer.train(req, deadline_ns=deadline_ns)

    assert artifact1.sha256 == artifact2.sha256
    assert artifact1.artifact_id == artifact2.artifact_id
    assert dossier1.training_metrics == dossier2.training_metrics


def test_real_trainer_deadline_enforcement(tmp_path: Path) -> None:
    """A deadline in the past must raise TrainingFailure with 'timeout'."""
    from quant_foundry.real_trainer import RealLightGBMTrainer
    from quant_foundry.runpod_training import TrainingFailure

    data_path = _make_test_dataset(tmp_path)
    req = _make_training_request(
        "qf:train:dl:1",
        data_path.as_uri(),
        seed=42,
    )

    trainer = RealLightGBMTrainer()
    past_deadline = time.time_ns() - 1

    with pytest.raises(TrainingFailure, match=r"timeout|deadline|time"):
        trainer.train(req, deadline_ns=past_deadline)


def test_real_trainer_shadow_only(tmp_path: Path) -> None:
    """The dossier must always carry Authority.SHADOW_ONLY."""
    from quant_foundry.real_trainer import RealLightGBMTrainer
    from quant_foundry.schemas import Authority

    data_path = _make_test_dataset(tmp_path)
    req = _make_training_request(
        "qf:train:auth:1",
        data_path.as_uri(),
        seed=42,
    )

    trainer = RealLightGBMTrainer()
    deadline_ns = time.time_ns() + 120 * 1_000_000_000
    artifact, dossier = trainer.train(req, deadline_ns=deadline_ns)

    assert dossier.authority == Authority.SHADOW_ONLY


def test_real_trainer_should_fail(tmp_path: Path) -> None:
    """should_fail=True must raise TrainingFailure."""
    from quant_foundry.real_trainer import RealLightGBMTrainer
    from quant_foundry.runpod_training import TrainingFailure

    data_path = _make_test_dataset(tmp_path)
    req = _make_training_request(
        "qf:train:fail:1",
        data_path.as_uri(),
        seed=42,
    )

    trainer = RealLightGBMTrainer(should_fail=True)
    deadline_ns = time.time_ns() + 120 * 1_000_000_000

    with pytest.raises(TrainingFailure, match=r"failure|error"):
        trainer.train(req, deadline_ns=deadline_ns)


def test_real_trainer_injected_into_handler(tmp_path: Path) -> None:
    """RealLightGBMTrainer can be injected into RunPodTrainingHandler."""
    from quant_foundry.real_trainer import RealLightGBMTrainer
    from quant_foundry.runpod_training import RunPodTrainingHandler
    from quant_foundry.schemas import (
        ArtifactManifest,
        Authority,
        ModelDossier,
        RunPodCallbackEnvelope,
    )
    from quant_foundry.signatures import verify_callback

    data_path = _make_test_dataset(tmp_path)
    secret = "test-real-trainer-secret"
    handler = RunPodTrainingHandler(
        callback_secret=secret,
        trainer=RealLightGBMTrainer(),
        deadline_seconds=120,
    )

    req = _make_training_request(
        "qf:train:inject:1",
        data_path.as_uri(),
        seed=42,
    )

    result = handler.handle(req)

    envelope = RunPodCallbackEnvelope.model_validate(
        json.loads(result.callback_payload),
    )
    assert envelope.job_id == "qf:train:inject:1"
    assert envelope.result_type == "training_complete"

    dossier = ModelDossier.model_validate(envelope.payload["dossier"])
    artifact = ArtifactManifest.model_validate(envelope.payload["artifact_manifest"])

    assert dossier.authority == Authority.SHADOW_ONLY
    assert len(artifact.sha256) == 64
    assert artifact.size_bytes > 0

    assert verify_callback(
        result.callback_payload,
        result.callback_signature,
        secret=secret,
        ts=result.callback_ts,
        job_id="qf:train:inject:1",
    )


def test_real_trainer_handler_no_broker_credentials(tmp_path: Path) -> None:
    """Handler with RealLightGBMTrainer must not have broker/Redis/stream attrs."""
    from quant_foundry.real_trainer import RealLightGBMTrainer
    from quant_foundry.runpod_training import RunPodTrainingHandler

    handler = RunPodTrainingHandler(
        callback_secret="s",
        trainer=RealLightGBMTrainer(),
    )
    for attr in (
        "redis", "broker", "bus", "producer", "stream",
        "sig_predict_writer", "order_writer", "trading_stream",
        "FINCEPT_JWT_SECRET", "ALPACA_API_KEY",
    ):
        assert not hasattr(handler, attr), f"handler must not have {attr}"


def test_real_trainer_metadata_contains_trainer_tag(tmp_path: Path) -> None:
    """The dossier metadata should mark the trainer as 'real_lightgbm'."""
    from quant_foundry.real_trainer import RealLightGBMTrainer

    data_path = _make_test_dataset(tmp_path)
    req = _make_training_request(
        "qf:train:meta:1",
        data_path.as_uri(),
        seed=42,
    )

    trainer = RealLightGBMTrainer()
    deadline_ns = time.time_ns() + 120 * 1_000_000_000
    artifact, dossier = trainer.train(req, deadline_ns=deadline_ns)

    assert dossier.metadata.get("trainer") == "real_lightgbm"
    assert dossier.metadata.get("model_family") == "gbm"
    assert "n_features" in dossier.metadata
    assert "n_rows" in dossier.metadata
    assert "brier_score" in dossier.metadata


def test_real_trainer_pbo_in_valid_range(tmp_path: Path) -> None:
    """PBO must be in [0, 1] (schema constraint)."""
    from quant_foundry.real_trainer import RealLightGBMTrainer

    data_path = _make_test_dataset(tmp_path)
    req = _make_training_request(
        "qf:train:pbo:1",
        data_path.as_uri(),
        seed=42,
    )

    trainer = RealLightGBMTrainer()
    deadline_ns = time.time_ns() + 120 * 1_000_000_000
    artifact, dossier = trainer.train(req, deadline_ns=deadline_ns)

    assert dossier.pbo is not None
    assert 0.0 <= dossier.pbo <= 1.0


# --- backward compat: LocalTrainer still works ------------------------------


def test_local_trainer_still_works() -> None:
    """LocalTrainer must still work unchanged (backward compat)."""
    from quant_foundry.runpod_training import LocalTrainer, RunPodTrainingHandler
    from quant_foundry.schemas import (
        ArtifactManifest,
        Authority,
        ModelDossier,
        RunPodCallbackEnvelope,
    )
    from quant_foundry.signatures import verify_callback

    secret = "local-compat-secret"
    handler = RunPodTrainingHandler(callback_secret=secret)

    from quant_foundry.schemas import RunPodTrainingRequest

    req = RunPodTrainingRequest(
        job_id="qf:train:compat:1",
        dataset_manifest_ref="ds-manifest-1",
        model_family="gbm",
        search_space={"n_estimators": [100, 200]},
        random_seed=42,
        hardware_class="mock-gpu",
        extra_constraints={},
    )

    result = handler.handle(req)

    envelope = RunPodCallbackEnvelope.model_validate(
        json.loads(result.callback_payload),
    )
    dossier = ModelDossier.model_validate(envelope.payload["dossier"])
    artifact = ArtifactManifest.model_validate(envelope.payload["artifact_manifest"])

    assert dossier.authority == Authority.SHADOW_ONLY
    assert artifact.sha256
    assert verify_callback(
        result.callback_payload,
        result.callback_signature,
        secret=secret,
        ts=result.callback_ts,
        job_id="qf:train:compat:1",
    )


def test_local_trainer_default_when_no_trainer_specified() -> None:
    """When no trainer is specified, the handler defaults to LocalTrainer."""
    from quant_foundry.runpod_training import LocalTrainer, RunPodTrainingHandler

    handler = RunPodTrainingHandler(callback_secret="s")
    assert isinstance(handler.trainer, LocalTrainer)
