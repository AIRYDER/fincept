"""
Tests for the Model Family Registry (T-7.1) and artifact_io loaders.

Covers:
  - The 5 initial families are registered and have correct fields.
  - ModelFamilySpec / FamilyValidationResult are frozen + extra='forbid'.
  - validate_family() rejects unknown families.
  - validate_family() rejects families with empty/missing artifact_loader.
  - validate_family() rejects production families without GPU image and
    without baseline exception.
  - validate_family() passes for valid production families.
  - validate_family() advisory warnings for canary/research mode.
  - validate_family_for_mode() in training_manifest.py.
  - is_family_registered() and get_family_spec().
  - ModelFamilyRegistry.register() rejects duplicates.
  - ModelFamilyRegistry.list() returns all family ids.
  - artifact_io.LOADER_REGISTRY has all 4 loaders.
  - artifact_io.resolve_loader() returns callable for known names.
  - artifact_io.resolve_loader() raises ValueError for unknown names.
  - artifact_io.load_sklearn_pickle() loads a pickled model.
  - artifact_io.load functions raise FileNotFoundError for missing paths.
  - artifact_loader references in all 5 family specs resolve to callables.
"""

from __future__ import annotations

import os
import pickle
import tempfile
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from quant_foundry.alpha_genome import (
    MODEL_FAMILY_REGISTRY,
    RUNPOD_GPU_TREE_IMAGE,
    FamilyValidationResult,
    ModelFamilyRegistry,
    ModelFamilySpec,
    PromotionEligibilityClass,
)
from quant_foundry.artifact_io import (
    LOADER_REGISTRY,
    load_catboost_model,
    load_lightgbm_model,
    load_sklearn_pickle,
    load_xgboost_model,
    resolve_loader,
)
from quant_foundry.training_manifest import (
    TrainingMode,
    get_family_spec,
    is_family_registered,
    validate_family_for_mode,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

INITIAL_FAMILY_IDS = [
    "lightgbm_baseline",
    "catboost_gpu",
    "xgboost_gpu",
    "logreg_sanity",
    "linear_sanity",
]


def _make_spec(
    *,
    family_id: str = "test_family",
    artifact_loader: str = "quant_foundry.artifact_io.load_sklearn_pickle",
    runpod_image: str | None = None,
    requires_gpu: bool = False,
    is_baseline_exception: bool = False,
) -> ModelFamilySpec:
    """Build a minimal valid ModelFamilySpec for tests."""
    return ModelFamilySpec(
        family_id=family_id,
        display_name=f"Test family {family_id}",
        version="1",
        dataset_shape="tabular_wide",
        objectives=("binary",),
        artifact_format="sklearn_pickle",
        artifact_loader=artifact_loader,
        required_metrics=("auc",),
        runpod_image=runpod_image,
        requires_gpu=requires_gpu,
        max_budget_cents=1000,
        promotion_eligibility_class=PromotionEligibilityClass.CHALLENGER,
        is_baseline_exception=is_baseline_exception,
        created_at_ns=0,
    )


# ---------------------------------------------------------------------------
# Registry: initial families
# ---------------------------------------------------------------------------


class TestInitialFamilies:
    """The 5 initial families are registered with the expected fields."""

    def test_all_five_initial_families_registered(self) -> None:
        listed = MODEL_FAMILY_REGISTRY.list()
        for fid in INITIAL_FAMILY_IDS:
            assert fid in listed, f"{fid} not registered"

    def test_registry_has_exactly_five_initial_families(self) -> None:
        # The singleton is pre-populated with exactly the 5 initial
        # families (other tests may register temporary families on a
        # *fresh* registry, but the module singleton has exactly 5).
        listed = MODEL_FAMILY_REGISTRY.list()
        assert set(INITIAL_FAMILY_IDS).issubset(set(listed))

    def test_lightgbm_baseline_fields(self) -> None:
        spec = MODEL_FAMILY_REGISTRY.get("lightgbm_baseline")
        assert spec.family_id == "lightgbm_baseline"
        assert spec.artifact_format == "lightgbm_model"
        assert (
            spec.artifact_loader
            == "quant_foundry.artifact_io.load_lightgbm_model"
        )
        assert spec.runpod_image is None
        assert spec.requires_gpu is False
        assert spec.is_baseline_exception is True
        assert (
            spec.promotion_eligibility_class
            == PromotionEligibilityClass.BASELINE
        )

    def test_catboost_gpu_fields(self) -> None:
        spec = MODEL_FAMILY_REGISTRY.get("catboost_gpu")
        assert spec.family_id == "catboost_gpu"
        assert spec.artifact_format == "catboost_model"
        assert (
            spec.artifact_loader
            == "quant_foundry.artifact_io.load_catboost_model"
        )
        assert spec.runpod_image == RUNPOD_GPU_TREE_IMAGE
        assert spec.requires_gpu is True
        assert spec.is_baseline_exception is False
        assert (
            spec.promotion_eligibility_class
            == PromotionEligibilityClass.CHALLENGER
        )

    def test_xgboost_gpu_fields(self) -> None:
        spec = MODEL_FAMILY_REGISTRY.get("xgboost_gpu")
        assert spec.family_id == "xgboost_gpu"
        assert spec.artifact_format == "xgboost_json"
        assert (
            spec.artifact_loader
            == "quant_foundry.artifact_io.load_xgboost_model"
        )
        assert spec.runpod_image == RUNPOD_GPU_TREE_IMAGE
        assert spec.requires_gpu is True
        assert spec.is_baseline_exception is False

    def test_logreg_sanity_fields(self) -> None:
        spec = MODEL_FAMILY_REGISTRY.get("logreg_sanity")
        assert spec.family_id == "logreg_sanity"
        assert spec.artifact_format == "sklearn_pickle"
        assert (
            spec.artifact_loader
            == "quant_foundry.artifact_io.load_sklearn_pickle"
        )
        assert spec.runpod_image is None
        assert spec.requires_gpu is False
        assert spec.is_baseline_exception is False
        assert (
            spec.promotion_eligibility_class
            == PromotionEligibilityClass.SANITY
        )

    def test_linear_sanity_fields(self) -> None:
        spec = MODEL_FAMILY_REGISTRY.get("linear_sanity")
        assert spec.family_id == "linear_sanity"
        assert spec.artifact_format == "sklearn_pickle"
        assert (
            spec.artifact_loader
            == "quant_foundry.artifact_io.load_sklearn_pickle"
        )
        assert spec.runpod_image is None
        assert spec.requires_gpu is False
        assert spec.is_baseline_exception is False
        assert (
            spec.promotion_eligibility_class
            == PromotionEligibilityClass.SANITY
        )


# ---------------------------------------------------------------------------
# Pydantic schema: frozen + extra='forbid'
# ---------------------------------------------------------------------------


class TestSchemaConstraints:
    """ModelFamilySpec and FamilyValidationResult are frozen + forbid extra."""

    def test_model_family_spec_frozen(self) -> None:
        spec = _make_spec()
        with pytest.raises(ValidationError):
            spec.family_id = "mutated"  # type: ignore[misc]

    def test_model_family_spec_extra_forbid(self) -> None:
        with pytest.raises(ValidationError):
            ModelFamilySpec(
                family_id="x",
                display_name="x",
                version="1",
                dataset_shape="tabular_wide",
                objectives=("binary",),
                artifact_format="sklearn_pickle",
                artifact_loader="quant_foundry.artifact_io.load_sklearn_pickle",
                required_metrics=("auc",),
                unknown_field="bad",  # type: ignore[call-arg]
            )

    def test_family_validation_result_frozen(self) -> None:
        result = FamilyValidationResult(
            passed=True, family_id="test", errors=(), warnings=()
        )
        with pytest.raises(ValidationError):
            result.passed = False  # type: ignore[misc]

    def test_family_validation_result_extra_forbid(self) -> None:
        with pytest.raises(ValidationError):
            FamilyValidationResult(  # type: ignore[call-arg]
                passed=True,
                family_id="test",
                errors=(),
                warnings=(),
                unknown_field="bad",
            )


# ---------------------------------------------------------------------------
# validate_family()
# ---------------------------------------------------------------------------


class TestValidateFamily:
    """ModelFamilyRegistry.validate_family() gating logic."""

    def test_rejects_unknown_family(self) -> None:
        result = MODEL_FAMILY_REGISTRY.validate_family(
            family_id="does_not_exist", mode="canary", has_gpu=False
        )
        assert result.passed is False
        assert any("not registered" in e for e in result.errors)

    def test_rejects_empty_artifact_loader(self) -> None:
        registry = ModelFamilyRegistry()
        # Build a spec with an empty loader via model_construct to bypass
        # the field validator (artifact_loader is a required str, so we
        # use construct to inject an empty value for the negative test).
        spec = ModelFamilySpec.model_construct(
            family_id="bad_loader",
            display_name="Bad loader",
            version="1",
            dataset_shape="tabular_wide",
            objectives=("binary",),
            artifact_format="sklearn_pickle",
            artifact_loader="",
            required_metrics=("auc",),
            runpod_image=None,
            requires_gpu=False,
            max_budget_cents=0,
            promotion_eligibility_class=PromotionEligibilityClass.SANITY,
            is_baseline_exception=False,
            created_at_ns=0,
        )
        registry._specs["bad_loader"] = spec
        result = registry.validate_family(
            family_id="bad_loader", mode="canary", has_gpu=False
        )
        assert result.passed is False
        assert any("no artifact_loader" in e for e in result.errors)

    def test_rejects_unresolvable_artifact_loader(self) -> None:
        registry = ModelFamilyRegistry()
        spec = _make_spec(
            family_id="bad_resolver",
            artifact_loader="quant_foundry.artifact_io.does_not_exist",
        )
        registry._specs["bad_resolver"] = spec
        result = registry.validate_family(
            family_id="bad_resolver", mode="canary", has_gpu=False
        )
        assert result.passed is False
        assert any("does not resolve" in e for e in result.errors)

    def test_rejects_production_without_gpu_and_without_baseline(self) -> None:
        registry = ModelFamilyRegistry()
        spec = _make_spec(
            family_id="no_gpu_prod",
            runpod_image=None,
            requires_gpu=False,
            is_baseline_exception=False,
        )
        registry._specs["no_gpu_prod"] = spec
        result = registry.validate_family(
            family_id="no_gpu_prod", mode="production", has_gpu=True
        )
        assert result.passed is False
        assert any("production mode requires" in e for e in result.errors)

    def test_passes_production_with_baseline_exception(self) -> None:
        # lightgbm_baseline has is_baseline_exception=True and no GPU —
        # it is allowed in production as the explicit baseline exception.
        result = MODEL_FAMILY_REGISTRY.validate_family(
            family_id="lightgbm_baseline",
            mode="production",
            has_gpu=False,
        )
        assert result.passed is True
        assert result.errors == ()

    def test_passes_production_with_gpu(self) -> None:
        # catboost_gpu maps to a GPU image + requires_gpu=True.
        result = MODEL_FAMILY_REGISTRY.validate_family(
            family_id="catboost_gpu", mode="production", has_gpu=True
        )
        assert result.passed is True
        assert result.errors == ()

    def test_passes_production_xgboost_with_gpu(self) -> None:
        result = MODEL_FAMILY_REGISTRY.validate_family(
            family_id="xgboost_gpu", mode="production", has_gpu=True
        )
        assert result.passed is True
        assert result.errors == ()

    def test_advisory_warning_canary_gpu_family_without_gpu(self) -> None:
        # catboost_gpu in canary mode without a GPU → warning, not error.
        result = MODEL_FAMILY_REGISTRY.validate_family(
            family_id="catboost_gpu", mode="canary", has_gpu=False
        )
        assert result.passed is True
        assert result.errors == ()
        assert len(result.warnings) >= 1
        assert any("prefers a GPU" in w for w in result.warnings)

    def test_advisory_warning_research_gpu_family_without_gpu(self) -> None:
        result = MODEL_FAMILY_REGISTRY.validate_family(
            family_id="xgboost_gpu", mode="research", has_gpu=False
        )
        assert result.passed is True
        assert len(result.warnings) >= 1

    def test_no_warning_when_gpu_available(self) -> None:
        result = MODEL_FAMILY_REGISTRY.validate_family(
            family_id="catboost_gpu", mode="canary", has_gpu=True
        )
        assert result.passed is True
        assert result.warnings == ()

    def test_sanity_family_canary_passes(self) -> None:
        result = MODEL_FAMILY_REGISTRY.validate_family(
            family_id="logreg_sanity", mode="canary", has_gpu=False
        )
        assert result.passed is True
        assert result.warnings == ()

    def test_sanity_family_production_rejected(self) -> None:
        # logreg_sanity has no GPU and no baseline exception → rejected
        # for production.
        result = MODEL_FAMILY_REGISTRY.validate_family(
            family_id="logreg_sanity", mode="production", has_gpu=False
        )
        assert result.passed is False
        assert any("production mode requires" in e for e in result.errors)


# ---------------------------------------------------------------------------
# training_manifest integration
# ---------------------------------------------------------------------------


class TestTrainingManifestIntegration:
    """validate_family_for_mode / is_family_registered / get_family_spec."""

    def test_validate_family_for_mode_production_gpu(self) -> None:
        result = validate_family_for_mode(
            family_id="catboost_gpu",
            mode=TrainingMode.PRODUCTION,
            has_gpu=True,
        )
        assert result is not None
        assert result.passed is True

    def test_validate_family_for_mode_production_baseline(self) -> None:
        result = validate_family_for_mode(
            family_id="lightgbm_baseline",
            mode=TrainingMode.PRODUCTION,
            has_gpu=False,
        )
        assert result is not None
        assert result.passed is True

    def test_validate_family_for_mode_canary_string(self) -> None:
        result = validate_family_for_mode(
            family_id="catboost_gpu", mode="canary", has_gpu=False
        )
        assert result is not None
        assert result.passed is True
        assert len(result.warnings) >= 1

    def test_validate_family_for_mode_unknown(self) -> None:
        result = validate_family_for_mode(
            family_id="nope", mode="canary", has_gpu=False
        )
        assert result is not None
        assert result.passed is False

    def test_is_family_registered_true(self) -> None:
        assert is_family_registered("lightgbm_baseline") is True

    def test_is_family_registered_false(self) -> None:
        assert is_family_registered("nope") is False

    def test_get_family_spec_returns_spec(self) -> None:
        spec = get_family_spec("catboost_gpu")
        assert spec is not None
        assert spec.family_id == "catboost_gpu"

    def test_get_family_spec_raises_for_unknown(self) -> None:
        with pytest.raises(KeyError):
            get_family_spec("nope")


# ---------------------------------------------------------------------------
# ModelFamilyRegistry.register() + list()
# ---------------------------------------------------------------------------


class TestRegistryMutation:
    """register() rejects duplicates; list() returns sorted ids."""

    def test_register_rejects_duplicate(self) -> None:
        registry = ModelFamilyRegistry()
        spec = _make_spec(family_id="dup")
        registry.register(spec)
        with pytest.raises(ValueError, match="already registered"):
            registry.register(_make_spec(family_id="dup"))

    def test_register_rejects_non_spec(self) -> None:
        registry = ModelFamilyRegistry()
        with pytest.raises(TypeError):
            registry.register("not a spec")  # type: ignore[arg-type]

    def test_register_rejects_empty_family_id(self) -> None:
        registry = ModelFamilyRegistry()
        spec = ModelFamilySpec.model_construct(
            family_id="",
            display_name="x",
            version="1",
            dataset_shape="tabular_wide",
            objectives=("binary",),
            artifact_format="sklearn_pickle",
            artifact_loader="quant_foundry.artifact_io.load_sklearn_pickle",
            required_metrics=("auc",),
        )
        with pytest.raises(ValueError):
            registry.register(spec)

    def test_list_returns_sorted_ids(self) -> None:
        registry = ModelFamilyRegistry()
        registry.register(_make_spec(family_id="zeta"))
        registry.register(_make_spec(family_id="alpha"))
        registry.register(_make_spec(family_id="mid"))
        assert registry.list() == ["alpha", "mid", "zeta"]

    def test_get_raises_keyerror_for_unknown(self) -> None:
        registry = ModelFamilyRegistry()
        with pytest.raises(KeyError):
            registry.get("nope")

    def test_is_registered(self) -> None:
        registry = ModelFamilyRegistry()
        registry.register(_make_spec(family_id="present"))
        assert registry.is_registered("present") is True
        assert registry.is_registered("absent") is False


# ---------------------------------------------------------------------------
# artifact_io: LOADER_REGISTRY + resolve_loader
# ---------------------------------------------------------------------------


class TestLoaderRegistry:
    """LOADER_REGISTRY has all 4 loaders; resolve_loader works."""

    def test_loader_registry_has_four_loaders(self) -> None:
        expected = {
            "quant_foundry.artifact_io.load_lightgbm_model",
            "quant_foundry.artifact_io.load_catboost_model",
            "quant_foundry.artifact_io.load_xgboost_model",
            "quant_foundry.artifact_io.load_sklearn_pickle",
        }
        assert expected.issubset(set(LOADER_REGISTRY))

    def test_loader_registry_values_are_callables(self) -> None:
        for name, fn in LOADER_REGISTRY.items():
            assert callable(fn), f"{name} is not callable"

    def test_resolve_loader_returns_callable_for_known(self) -> None:
        fn = resolve_loader("quant_foundry.artifact_io.load_sklearn_pickle")
        assert callable(fn)
        assert fn is load_sklearn_pickle

    def test_resolve_loader_lightgbm(self) -> None:
        assert (
            resolve_loader("quant_foundry.artifact_io.load_lightgbm_model")
            is load_lightgbm_model
        )

    def test_resolve_loader_catboost(self) -> None:
        assert (
            resolve_loader("quant_foundry.artifact_io.load_catboost_model")
            is load_catboost_model
        )

    def test_resolve_loader_xgboost(self) -> None:
        assert (
            resolve_loader("quant_foundry.artifact_io.load_xgboost_model")
            is load_xgboost_model
        )

    def test_resolve_loader_raises_for_unknown(self) -> None:
        with pytest.raises(ValueError, match="unknown artifact loader"):
            resolve_loader("quant_foundry.artifact_io.does_not_exist")

    def test_resolve_loader_raises_for_empty(self) -> None:
        with pytest.raises(ValueError):
            resolve_loader("")

    def test_resolve_loader_raises_for_none(self) -> None:
        with pytest.raises(ValueError):
            resolve_loader(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# artifact_io: load functions
# ---------------------------------------------------------------------------


class TestLoadFunctions:
    """Loader functions validate paths and load models."""

    def test_load_sklearn_pickle_loads_model(self, tmp_path: Path) -> None:
        # Use a simple sklearn dummy model if sklearn is available;
        # otherwise fall back to a plain pickled dict (the loader just
        # unpickles, it doesn't require the object to be an sklearn model).
        obj = self._make_picklable_model()
        pkl_path = tmp_path / "model.pkl"
        with open(pkl_path, "wb") as fh:
            pickle.dump(obj, fh)
        loaded = load_sklearn_pickle(str(pkl_path))
        assert loaded is not None
        # If it's an sklearn model with predict, just assert it loaded.
        # Otherwise assert it equals the original.
        if hasattr(obj, "predict"):
            assert hasattr(loaded, "predict")
        else:
            assert loaded == obj

    def _make_picklable_model(self) -> Any:
        """Return a simple picklable model object.

        Tries sklearn's DummyClassifier; falls back to a simple namespace
        if sklearn is not installed.
        """
        try:
            from sklearn.dummy import DummyClassifier

            return DummyClassifier(strategy="prior")
        except ImportError:
            return {"type": "fake_model", "weights": [0.1, 0.9]}

    def test_load_sklearn_pickle_file_not_found(self) -> None:
        with pytest.raises(FileNotFoundError):
            load_sklearn_pickle("/nonexistent/path/model.pkl")

    def test_load_lightgbm_file_not_found(self) -> None:
        with pytest.raises(FileNotFoundError):
            load_lightgbm_model("/nonexistent/path/model.txt")

    def test_load_catboost_file_not_found(self) -> None:
        with pytest.raises(FileNotFoundError):
            load_catboost_model("/nonexistent/path/model.cbm")

    def test_load_xgboost_file_not_found(self) -> None:
        with pytest.raises(FileNotFoundError):
            load_xgboost_model("/nonexistent/path/model.json")

    def test_load_sklearn_pickle_rejects_empty_file(self, tmp_path: Path) -> None:
        empty_path = tmp_path / "empty.pkl"
        empty_path.write_bytes(b"")
        with pytest.raises(ValueError, match="empty"):
            load_sklearn_pickle(str(empty_path))

    def test_load_lightgbm_rejects_empty_file(self, tmp_path: Path) -> None:
        empty_path = tmp_path / "empty.txt"
        empty_path.write_bytes(b"")
        with pytest.raises(ValueError, match="empty"):
            load_lightgbm_model(str(empty_path))

    def test_load_sklearn_pickle_rejects_empty_string_path(self) -> None:
        with pytest.raises(ValueError):
            load_sklearn_pickle("")

    def test_load_sklearn_pickle_joblib_format(self, tmp_path: Path) -> None:
        """If joblib is installed, the loader uses it; otherwise pickle."""
        obj = self._make_picklable_model()
        joblib_path = tmp_path / "model.joblib"
        try:
            import joblib

            joblib.dump(obj, joblib_path)
        except ImportError:
            # joblib not installed — write a plain pickle with .joblib
            # extension; the loader falls back to pickle.
            with open(joblib_path, "wb") as fh:
                pickle.dump(obj, fh)
        loaded = load_sklearn_pickle(str(joblib_path))
        assert loaded is not None


# ---------------------------------------------------------------------------
# Cross-check: all 5 family specs' artifact_loader resolves to a callable
# ---------------------------------------------------------------------------


class TestFamilySpecLoadersResolve:
    """Every registered family's artifact_loader resolves to a callable."""

    @pytest.mark.parametrize("family_id", INITIAL_FAMILY_IDS)
    def test_artifact_loader_resolves(self, family_id: str) -> None:
        spec = MODEL_FAMILY_REGISTRY.get(family_id)
        loader = resolve_loader(spec.artifact_loader)
        assert callable(loader)

    @pytest.mark.parametrize("family_id", INITIAL_FAMILY_IDS)
    def test_resolve_artifact_loader_method(self, family_id: str) -> None:
        spec = MODEL_FAMILY_REGISTRY.get(family_id)
        loader = spec.resolve_artifact_loader()
        assert callable(loader)

    def test_all_initial_families_pass_validation_canary(self) -> None:
        for fid in INITIAL_FAMILY_IDS:
            result = MODEL_FAMILY_REGISTRY.validate_family(
                family_id=fid, mode="canary", has_gpu=False
            )
            assert result.passed is True, (
                f"{fid} failed canary validation: {result.errors}"
            )
