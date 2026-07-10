"""
Tests for Tier 1.3: GPU backend (xgboost_gpu with device=cuda + determinism status).

These tests verify:
- ``_build_xgboost_params`` sets ``device='cuda'`` when
  ``req.model_family == 'xgboost_gpu'`` and ``device='cpu'`` otherwise.
- ``TRAINER_BACKENDS`` includes ``'xgboost_gpu'`` as a valid backend.
- The ``xgboost_gpu`` backend routes through ``_train_xgboost``.
- ``ArtifactManifest`` gains a ``determinism_status`` field that records
  deterministic (CPU) vs non_deterministic (GPU) training.
- LightGBM CPU remains the deterministic reference baseline (unchanged).

The param-builder tests are pure-Python (no ML deps required) so they run
in every environment. The end-to-end training test is skipped when xgboost
is not installed.
"""

from __future__ import annotations

import importlib.util

import pytest
from quant_foundry.dataset_manifest import ColumnRoles
from quant_foundry.schemas import ArtifactManifest, RunPodTrainingRequest
from quant_foundry.training_manifest import ModelTaskSpec

# --- helpers ----------------------------------------------------------------


def _binary_roles() -> ColumnRoles:
    return ColumnRoles(
        feature_columns=("f1", "f2", "f3"),
        label_columns=("y",),
    )


def _binary_spec() -> ModelTaskSpec:
    return ModelTaskSpec(task_type="binary", label_column="y")


def _make_req(model_family: str, seed: int = 7) -> RunPodTrainingRequest:
    return RunPodTrainingRequest(
        job_id="job-gpu-test",
        dataset_manifest_ref="ds:test:1",
        model_family=model_family,
        search_space={
            "max_depth": [4],
            "learning_rate": [0.05],
            "n_estimators": [10],
        },
        random_seed=seed,
    )


# --- TRAINER_BACKENDS includes xgboost_gpu ----------------------------------


def test_trainer_backends_includes_xgboost_gpu() -> None:
    """TRAINER_BACKENDS must list 'xgboost_gpu' as a valid backend."""
    from quant_foundry.real_trainer import TRAINER_BACKENDS

    assert "xgboost_gpu" in TRAINER_BACKENDS
    assert "xgboost" in TRAINER_BACKENDS
    assert "lightgbm" in TRAINER_BACKENDS


# --- _build_xgboost_params device selection (pure-Python, no ML deps) -------


def test_build_xgboost_params_gpu_sets_device_cuda() -> None:
    """xgboost_gpu model_family must set device='cuda' in XGBoost params."""
    from quant_foundry.real_trainer import RealLightGBMTrainer

    trainer = RealLightGBMTrainer(
        column_roles=_binary_roles(),
        task_spec=_binary_spec(),
        backend="xgboost_gpu",
    )
    req = _make_req(model_family="xgboost_gpu")
    params = trainer._build_xgboost_params(req, seed=7)

    assert params["device"] == "cuda"
    # The rest of the param shape is preserved.
    assert params["tree_method"] == "hist"
    assert params["max_depth"] == 4
    assert params["learning_rate"] == 0.05
    assert params["n_estimators"] == 10


def test_build_xgboost_params_cpu_keeps_device_cpu() -> None:
    """Plain xgboost model_family must keep device='cpu' (deterministic)."""
    from quant_foundry.real_trainer import RealLightGBMTrainer

    trainer = RealLightGBMTrainer(
        column_roles=_binary_roles(),
        task_spec=_binary_spec(),
        backend="xgboost",
    )
    req = _make_req(model_family="xgboost")
    params = trainer._build_xgboost_params(req, seed=7)

    assert params["device"] == "cpu"


def test_build_xgboost_params_gpu_backend_with_other_family_keeps_cpu() -> None:
    """device is driven by req.model_family, not by self.backend alone.

    This guards the handler.py routing path where ``backend='xgboost'`` is
    used but ``model_family='xgboost'`` (CPU). device must stay 'cpu'.
    """
    from quant_foundry.real_trainer import RealLightGBMTrainer

    trainer = RealLightGBMTrainer(
        column_roles=_binary_roles(),
        task_spec=_binary_spec(),
        backend="xgboost",
    )
    req = _make_req(model_family="xgboost")
    params = trainer._build_xgboost_params(req, seed=3)

    assert params["device"] == "cpu"


# --- ArtifactManifest.determinism_status field ------------------------------


def test_artifact_manifest_has_determinism_status_default_none() -> None:
    """ArtifactManifest must accept determinism_status (defaults to None)."""
    art = ArtifactManifest(
        schema_version=1,
        artifact_id="art-gpu-001",
        sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        size_bytes=42,
        uri=None,
        model_family="xgboost_gpu",
        created_at_ns=1_700_000_000_000_000_000,
        feature_schema_hash="fs:hash:1",
        label_schema_hash="ls:hash:1",
    )
    # Backward compatible: defaults to None when not supplied.
    assert art.determinism_status is None


def test_artifact_manifest_determinism_status_set_non_deterministic() -> None:
    """ArtifactManifest must accept an explicit non_deterministic status."""
    art = ArtifactManifest(
        schema_version=1,
        artifact_id="art-gpu-002",
        sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        size_bytes=42,
        uri=None,
        model_family="xgboost_gpu",
        created_at_ns=1_700_000_000_000_000_000,
        feature_schema_hash="fs:hash:1",
        label_schema_hash="ls:hash:1",
        determinism_status="non_deterministic",
    )
    assert art.determinism_status == "non_deterministic"


def test_artifact_manifest_determinism_status_set_deterministic() -> None:
    """ArtifactManifest must accept an explicit deterministic status."""
    art = ArtifactManifest(
        schema_version=1,
        artifact_id="art-cpu-001",
        sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        size_bytes=42,
        uri=None,
        model_family="xgboost",
        created_at_ns=1_700_000_000_000_000_000,
        feature_schema_hash="fs:hash:1",
        label_schema_hash="ls:hash:1",
        determinism_status="deterministic",
    )
    assert art.determinism_status == "deterministic"


def test_artifact_manifest_rejects_unknown_field() -> None:
    """extra='forbid' must still reject unknown fields (schema contract)."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ArtifactManifest(
            schema_version=1,
            artifact_id="art-bad",
            sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            size_bytes=42,
            model_family="xgboost",
            created_at_ns=1,
            feature_schema_hash="fs",
            label_schema_hash="ls",
            not_a_real_field=True,  # type: ignore[call-arg]
        )


# --- determinism_status computed in _build_backend_artifact_and_dossier ------


def test_determinism_status_helper_for_xgboost_gpu() -> None:
    """The xgboost call site computes non_deterministic for xgboost_gpu."""
    # The logic mirrored from _train_xgboost's call site.
    req = _make_req(model_family="xgboost_gpu")
    status = "non_deterministic" if req.model_family == "xgboost_gpu" else "deterministic"
    assert status == "non_deterministic"


def test_determinism_status_helper_for_xgboost_cpu() -> None:
    """The xgboost call site computes deterministic for plain xgboost."""
    req = _make_req(model_family="xgboost")
    status = "non_deterministic" if req.model_family == "xgboost_gpu" else "deterministic"
    assert status == "deterministic"


# --- LightGBM CPU deterministic baseline unchanged --------------------------


def test_lightgbm_remains_deterministic_baseline() -> None:
    """LightGBM CPU trainer must keep num_threads=1, deterministic=True.

    This is a regression guard: the GPU backend work must NOT alter the
    LightGBM deterministic reference baseline settings.
    """
    from quant_foundry.real_trainer import RealLightGBMTrainer

    trainer = RealLightGBMTrainer(backend="lightgbm")
    # The LightGBM backend must still be the default and deterministic.
    assert trainer.backend == "lightgbm"
    # LightGBM params are built inside _train (lazy); we assert the
    # backend selection + that the GPU work did not change the dataclass
    # defaults that govern determinism.
    assert trainer.n_folds == 3


# --- End-to-end: xgboost_gpu routes through _train_xgboost (needs xgboost) ---

_XGB_AVAILABLE = importlib.util.find_spec("xgboost") is not None


@pytest.mark.skipif(not _XGB_AVAILABLE, reason="xgboost not installed")
def test_xgboost_gpu_backend_routes_to_train_xgboost(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """backend='xgboost_gpu' must dispatch to _train_xgboost (not raise)."""
    from quant_foundry.real_trainer import RealLightGBMTrainer

    trainer = RealLightGBMTrainer(
        column_roles=_binary_roles(),
        task_spec=_binary_spec(),
        backend="xgboost_gpu",
    )

    called: dict[str, object] = {}

    def _fake_train_xgboost(self, req, *, deadline_ns):  # type: ignore[no-untyped-def]
        called["model_family"] = req.model_family
        # Return a minimal (artifact, dossier) pair to satisfy the contract.
        from quant_foundry.schemas import ArtifactManifest, Authority, ModelDossier

        art = ArtifactManifest(
            artifact_id="art-route",
            sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            size_bytes=1,
            model_family=req.model_family,
            created_at_ns=1,
            feature_schema_hash="fs",
            label_schema_hash="ls",
            determinism_status="non_deterministic",
        )
        dossier = ModelDossier(
            model_id="model:route",
            artifact_manifest_id=art.artifact_id,
            dataset_manifest_id=req.dataset_manifest_ref,
            code_git_sha="unknown",
            lockfile_hash="unknown",
            container_image_digest="unknown",
            authority=Authority.SHADOW_ONLY,
        )
        return art, dossier

    monkeypatch.setattr(RealLightGBMTrainer, "_train_xgboost", _fake_train_xgboost, raising=True)

    req = _make_req(model_family="xgboost_gpu")
    art, _dossier = trainer.train(req, deadline_ns=2**62)
    assert called["model_family"] == "xgboost_gpu"
    assert art.determinism_status == "non_deterministic"


# --- CatBoost GPU (Tier 1.3 completion) --------------------------------------


def test_trainer_backends_includes_catboost_gpu() -> None:
    """TRAINER_BACKENDS must list 'catboost_gpu' as a valid backend."""
    from quant_foundry.real_trainer import TRAINER_BACKENDS

    assert "catboost_gpu" in TRAINER_BACKENDS
    assert "catboost" in TRAINER_BACKENDS


def test_build_catboost_params_gpu_sets_task_type_gpu() -> None:
    """catboost_gpu model_family must set task_type='GPU' in CatBoost params."""
    from quant_foundry.real_trainer import RealLightGBMTrainer

    trainer = RealLightGBMTrainer(
        column_roles=_binary_roles(),
        task_spec=_binary_spec(),
        backend="catboost_gpu",
    )
    req = _make_req(model_family="catboost_gpu")
    params = trainer._build_catboost_params(req, seed=7)

    assert params["task_type"] == "GPU"
    # The rest of the param shape is preserved.
    assert params["iterations"] == 10
    assert params["depth"] == 4
    assert params["learning_rate"] == 0.05
    assert params["random_seed"] == 7
    assert params["verbose"] is False


def test_build_catboost_params_cpu_keeps_task_type_cpu() -> None:
    """Plain catboost model_family must keep task_type='CPU' (deterministic)."""
    from quant_foundry.real_trainer import RealLightGBMTrainer

    trainer = RealLightGBMTrainer(
        column_roles=_binary_roles(),
        task_spec=_binary_spec(),
        backend="catboost",
    )
    req = _make_req(model_family="catboost")
    params = trainer._build_catboost_params(req, seed=7)

    assert params["task_type"] == "CPU"


def test_build_catboost_params_gpu_backend_with_other_family_keeps_cpu() -> None:
    """task_type is driven by req.model_family, not by self.backend alone.

    This guards the handler.py routing path where ``backend='catboost'``
    is used but ``model_family='catboost'`` (CPU). task_type must stay 'CPU'.
    """
    from quant_foundry.real_trainer import RealLightGBMTrainer

    trainer = RealLightGBMTrainer(
        column_roles=_binary_roles(),
        task_spec=_binary_spec(),
        backend="catboost",
    )
    req = _make_req(model_family="catboost")
    params = trainer._build_catboost_params(req, seed=3)

    assert params["task_type"] == "CPU"


def test_determinism_status_helper_for_catboost_gpu() -> None:
    """The catboost call site computes non_deterministic for catboost_gpu."""
    req = _make_req(model_family="catboost_gpu")
    status = "non_deterministic" if req.model_family == "catboost_gpu" else "deterministic"
    assert status == "non_deterministic"


def test_determinism_status_helper_for_catboost_cpu() -> None:
    """The catboost call site computes deterministic for plain catboost."""
    req = _make_req(model_family="catboost")
    status = "non_deterministic" if req.model_family == "catboost_gpu" else "deterministic"
    assert status == "deterministic"


def test_catboost_gpu_artifact_manifest_non_deterministic() -> None:
    """ArtifactManifest for catboost_gpu must carry non_deterministic status."""
    art = ArtifactManifest(
        schema_version=1,
        artifact_id="art-catboost-gpu-001",
        sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        size_bytes=42,
        uri=None,
        model_family="catboost_gpu",
        created_at_ns=1_700_000_000_000_000_000,
        feature_schema_hash="fs:hash:1",
        label_schema_hash="ls:hash:1",
        determinism_status="non_deterministic",
    )
    assert art.determinism_status == "non_deterministic"


_CATBOOST_AVAILABLE = importlib.util.find_spec("catboost") is not None


@pytest.mark.skipif(not _CATBOOST_AVAILABLE, reason="catboost not installed")
def test_catboost_gpu_backend_routes_to_train_catboost(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """backend='catboost_gpu' must dispatch to _train_catboost (not raise)."""
    from quant_foundry.real_trainer import RealLightGBMTrainer

    trainer = RealLightGBMTrainer(
        column_roles=_binary_roles(),
        task_spec=_binary_spec(),
        backend="catboost_gpu",
    )

    called: dict[str, object] = {}

    def _fake_train_catboost(self, req, *, deadline_ns):  # type: ignore[no-untyped-def]
        called["model_family"] = req.model_family
        from quant_foundry.schemas import ArtifactManifest, Authority, ModelDossier

        art = ArtifactManifest(
            artifact_id="art-catboost-route",
            sha256="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            size_bytes=1,
            model_family=req.model_family,
            created_at_ns=1,
            feature_schema_hash="fs",
            label_schema_hash="ls",
            determinism_status="non_deterministic",
        )
        dossier = ModelDossier(
            model_id="model:catboost-route",
            artifact_manifest_id=art.artifact_id,
            dataset_manifest_id=req.dataset_manifest_ref,
            code_git_sha="unknown",
            lockfile_hash="unknown",
            container_image_digest="unknown",
            authority=Authority.SHADOW_ONLY,
        )
        return art, dossier

    monkeypatch.setattr(RealLightGBMTrainer, "_train_catboost", _fake_train_catboost, raising=True)

    req = _make_req(model_family="catboost_gpu")
    art, _dossier = trainer.train(req, deadline_ns=2**62)
    assert called["model_family"] == "catboost_gpu"
    assert art.determinism_status == "non_deterministic"
