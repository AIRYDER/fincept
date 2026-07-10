"""
TDD tests for RunPod training mode validation (T-0.1 / T-TV.1).

The model trainer improvement plan introduces three operating modes that
govern what a RunPod training job may do and what it must prove:

- ``canary``    : small registered dataset, may use tiny inline artifacts,
                  never promotion eligible by default.
- ``research``  : real RunPod training, experimental model families allowed,
                  promotion disabled unless explicitly escalated.
- ``production``: registered L3/L4 dataset, GPU required, artifact
                  verification required, quality gates required, no CPU
                  fallback.

Production mode is the strict path. It MUST enforce:

- ``gpu_required == True``
- ``allow_cpu_fallback == False``
- a registered dataset reference (not a raw CSV/parquet path)
- a non-empty ``quality_policy_id``
- ``artifact_verification_required == True``

These tests assert the mode contract at two layers:

1. **Manifest layer** — ``TrainingManifest._validate_mode_rules`` rejects
   construction of a production manifest that violates any rule.
2. **Dispatch layer** — ``quant_foundry.runpod_training.validate_mode``
   rejects a ``RunPodTrainingRequest`` whose ``extra_constraints`` violate
   the resolved mode's rules (the mode is forwarded via
   ``extra_constraints["training_mode"]`` by
   ``TrainingManifest.to_dispatch_request``).

The authoritative rules table is
:data:`quant_foundry.training_manifest.MODE_RULES` — the single source of
truth that both validators reference.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from quant_foundry.runpod_training import (
    ModeValidationError,
    validate_mode,
)
from quant_foundry.schemas import RunPodTrainingRequest
from quant_foundry.training_manifest import (
    MODE_RULES,
    TrainingManifest,
    TrainingMode,
)

NS_PER_DAY = 86_400_000_000_000
_VALID_LAKE_HASH = "a" * 64  # 64-char hex SHA-256 placeholder


# ---------------------------------------------------------------------------
# Helpers — manifest layer
# ---------------------------------------------------------------------------


def _base_manifest_kwargs(**overrides: object) -> dict[str, object]:
    """Return a minimal valid manifest dict (research defaults) + overrides."""
    base: dict[str, object] = {
        "manifest_id": "tm-mode-001",
        "feature_lake_manifest_ref": "ds-test",
        "feature_lake_manifest_hash": _VALID_LAKE_HASH,
        "model_family": "gbm",
        "hyperparameters": {"n_estimators": 100.0, "max_depth": 4.0, "learning_rate": 0.05},
        "train_window_ns": 30 * NS_PER_DAY,
        "val_window_ns": 10 * NS_PER_DAY,
        "test_window_ns": 10 * NS_PER_DAY,
        "label_horizon_ns": NS_PER_DAY,
        "random_seed": 42,
        "walk_forward_enabled": True,
        "budget_cents": 0,
        "timeout_seconds": 120,
        "operator_note": "mode test",
        "mode": TrainingMode.RESEARCH,
    }
    base.update(overrides)
    return base


def _canary_manifest(**overrides: object) -> TrainingManifest:
    return TrainingManifest(**_base_manifest_kwargs(mode=TrainingMode.CANARY, **overrides))


def _research_manifest(**overrides: object) -> TrainingManifest:
    return TrainingManifest(**_base_manifest_kwargs(mode=TrainingMode.RESEARCH, **overrides))


def _production_manifest(**overrides: object) -> TrainingManifest:
    """A fully-valid production manifest (all production constraints met)."""
    prod_kwargs: dict[str, object] = {
        "mode": TrainingMode.PRODUCTION,
        "gpu_required": True,
        "allow_cpu_fallback": False,
        "quality_policy_id": "qp-production-v1",
        "dataset_registry_ref": "ds-registry-l3-001",
        "artifact_verification_required": True,
    }
    prod_kwargs.update(overrides)
    return TrainingManifest(**_base_manifest_kwargs(**prod_kwargs))


# ---------------------------------------------------------------------------
# Helpers — dispatch layer (RunPodTrainingRequest)
# ---------------------------------------------------------------------------


def _make_request(
    *,
    mode: TrainingMode = TrainingMode.RESEARCH,
    dataset_manifest_ref: str = "ds-test:aaa",
    extra: dict[str, str] | None = None,
) -> RunPodTrainingRequest:
    """Build a RunPodTrainingRequest with mode + production controls in
    ``extra_constraints`` (the shape ``to_dispatch_request`` produces)."""
    ec: dict[str, str] = {"training_mode": mode.value}
    if extra:
        ec.update(extra)
    return RunPodTrainingRequest(
        job_id="qf:train:mode:test:1",
        dataset_manifest_ref=dataset_manifest_ref,
        model_family="gbm",
        search_space={"n_estimators": [100, 200]},
        random_seed=42,
        hardware_class="mock-gpu",
        extra_constraints=ec,
    )


def _production_request(
    *,
    dataset_manifest_ref: str = "ds-test:aaa",
    extra: dict[str, str] | None = None,
) -> RunPodTrainingRequest:
    """A fully-valid production request (all production constraints met)."""
    prod_extra: dict[str, str] = {
        "gpu_required": "1",
        "allow_cpu_fallback": "0",
        "quality_policy_id": "qp-production-v1",
        "artifact_verification_required": "1",
    }
    if extra:
        prod_extra.update(extra)
    return _make_request(
        mode=TrainingMode.PRODUCTION,
        dataset_manifest_ref=dataset_manifest_ref,
        extra=prod_extra,
    )


# ---------------------------------------------------------------------------
# TrainingMode enum + MODE_RULES table
# ---------------------------------------------------------------------------


class TestTrainingModeEnum:
    def test_mode_enum_has_three_members(self) -> None:
        values = {m.value for m in TrainingMode}
        assert {"canary", "research", "production"} <= values

    def test_canary_value(self) -> None:
        assert TrainingMode.CANARY.value == "canary"

    def test_research_value(self) -> None:
        assert TrainingMode.RESEARCH.value == "research"

    def test_production_value(self) -> None:
        assert TrainingMode.PRODUCTION.value == "production"

    def test_mode_rules_table_covers_all_modes(self) -> None:
        for mode in (TrainingMode.CANARY, TrainingMode.RESEARCH, TrainingMode.PRODUCTION):
            assert mode in MODE_RULES
            rules = MODE_RULES[mode]
            for key in (
                "gpu_required",
                "allow_cpu_fallback",
                "registered_dataset_required",
                "quality_policy_required",
                "artifact_verification_required",
                "promotion_eligible_default",
            ):
                assert key in rules, f"{mode} rules missing {key}"


# ---------------------------------------------------------------------------
# Canary mode
# ---------------------------------------------------------------------------


class TestCanaryMode:
    def test_canary_manifest_constructs(self) -> None:
        m = _canary_manifest()
        assert m.mode == TrainingMode.CANARY

    def test_canary_allows_small_dataset(self) -> None:
        """Canary must NOT require the production-only fields. A canary
        manifest with no gpu_required / quality_policy_id / artifact
        verification must still construct (lenient path)."""
        m = _canary_manifest()
        assert m.gpu_required is False
        assert m.allow_cpu_fallback is True
        assert m.quality_policy_id is None
        assert m.artifact_verification_required is False

    def test_canary_promotion_ineligible_by_default(self) -> None:
        """Canary is never promotion eligible by default (per MODE_RULES)."""
        assert MODE_RULES[TrainingMode.CANARY]["promotion_eligible_default"] is False

    def test_canary_does_not_require_gpu(self) -> None:
        """A canary manifest that explicitly disables GPU must construct."""
        m = _canary_manifest(gpu_required=False, allow_cpu_fallback=True)
        assert m.mode == TrainingMode.CANARY

    def test_canary_validate_mode_never_raises(self) -> None:
        """validate_mode is permissive for canary — returns the mode."""
        req = _make_request(mode=TrainingMode.CANARY)
        assert validate_mode(req) == TrainingMode.CANARY


# ---------------------------------------------------------------------------
# Research mode
# ---------------------------------------------------------------------------


class TestResearchMode:
    def test_research_manifest_constructs(self) -> None:
        m = _research_manifest()
        assert m.mode == TrainingMode.RESEARCH

    def test_research_promotion_disabled_by_default(self) -> None:
        """Research mode disables promotion unless explicitly escalated."""
        assert MODE_RULES[TrainingMode.RESEARCH]["promotion_eligible_default"] is False

    def test_research_is_permissive_no_production_constraints(self) -> None:
        """Research mode does NOT enforce the production-only constraints:
        GPU is not required, CPU fallback is allowed, no quality policy or
        registered dataset is required, and artifact verification is not
        mandatory. Experimental families are allowed at this layer."""
        rules = MODE_RULES[TrainingMode.RESEARCH]
        assert rules["gpu_required"] is False
        assert rules["allow_cpu_fallback"] is True
        assert rules["registered_dataset_required"] is False
        assert rules["quality_policy_required"] is False
        assert rules["artifact_verification_required"] is False

    def test_research_does_not_require_gpu(self) -> None:
        """Research may run without enforcing the production GPU contract."""
        m = _research_manifest(gpu_required=False, allow_cpu_fallback=True)
        assert m.mode == TrainingMode.RESEARCH

    def test_research_allows_experimental_family(self) -> None:
        """Research mode must accept a model family outside the strict
        baseline allowlist (experimental families allowed). Canary and
        production modes enforce the allowlist; research does not."""
        m = _research_manifest(model_family="experimental_transformer")
        assert m.mode == TrainingMode.RESEARCH
        assert m.model_family == "experimental_transformer"

    def test_canary_rejects_experimental_family(self) -> None:
        """Canary mode enforces the baseline family allowlist (no
        experimental families) — only research is permissive."""
        with pytest.raises((ValidationError, ValueError), match="allowlist"):
            _canary_manifest(model_family="experimental_transformer")

    def test_production_rejects_experimental_family(self) -> None:
        """Production mode enforces the baseline family allowlist."""
        with pytest.raises((ValidationError, ValueError), match="allowlist"):
            _production_manifest(model_family="experimental_transformer")

    def test_research_validate_mode_never_raises(self) -> None:
        """validate_mode is permissive for research — returns the mode."""
        req = _make_request(mode=TrainingMode.RESEARCH)
        assert validate_mode(req) == TrainingMode.RESEARCH


# ---------------------------------------------------------------------------
# Production mode — happy path
# ---------------------------------------------------------------------------


class TestProductionModeHappyPath:
    def test_valid_production_manifest_constructs(self) -> None:
        m = _production_manifest()
        assert m.mode == TrainingMode.PRODUCTION
        assert m.gpu_required is True
        assert m.allow_cpu_fallback is False
        assert m.quality_policy_id == "qp-production-v1"
        assert m.dataset_registry_ref == "ds-registry-l3-001"
        assert m.artifact_verification_required is True

    def test_valid_production_request_validates(self) -> None:
        req = _production_request()
        assert validate_mode(req) == TrainingMode.PRODUCTION

    def test_production_rules_are_strict(self) -> None:
        """The MODE_RULES table marks production as the strict path."""
        rules = MODE_RULES[TrainingMode.PRODUCTION]
        assert rules["gpu_required"] is True
        assert rules["allow_cpu_fallback"] is False
        assert rules["registered_dataset_required"] is True
        assert rules["quality_policy_required"] is True
        assert rules["artifact_verification_required"] is True
        assert rules["promotion_eligible_default"] is False


# ---------------------------------------------------------------------------
# Production mode — GPU required
# ---------------------------------------------------------------------------


class TestProductionGpuRequired:
    def test_manifest_rejects_gpu_required_false(self) -> None:
        with pytest.raises((ValidationError, ValueError)):
            _production_manifest(gpu_required=False)

    def test_manifest_rejects_gpu_required_missing(self) -> None:
        """Omitting gpu_required (default False) must fail in production."""
        kwargs = _base_manifest_kwargs(
            mode=TrainingMode.PRODUCTION,
            allow_cpu_fallback=False,
            quality_policy_id="qp-production-v1",
            artifact_verification_required=True,
        )
        kwargs.pop("gpu_required", None)
        with pytest.raises((ValidationError, ValueError)):
            TrainingManifest(**kwargs)

    def test_request_rejects_gpu_required_not_set(self) -> None:
        with pytest.raises(ModeValidationError, match="gpu_required"):
            validate_mode(_production_request(extra={"gpu_required": "0"}))

    def test_request_rejects_gpu_required_missing(self) -> None:
        """A production request without gpu_required in extra_constraints
        must be rejected by validate_mode."""
        req = _production_request()
        # Drop gpu_required so the key is absent.
        ec = dict(req.extra_constraints)
        ec.pop("gpu_required", None)
        req = req.model_copy(update={"extra_constraints": ec})
        with pytest.raises(ModeValidationError, match="gpu_required"):
            validate_mode(req)


# ---------------------------------------------------------------------------
# Production mode — no CPU fallback
# ---------------------------------------------------------------------------


class TestProductionNoCpuFallback:
    def test_manifest_rejects_allow_cpu_fallback_true(self) -> None:
        with pytest.raises((ValidationError, ValueError)):
            _production_manifest(allow_cpu_fallback=True)

    def test_manifest_rejects_allow_cpu_fallback_missing(self) -> None:
        """Omitting allow_cpu_fallback (default True) must fail in
        production."""
        kwargs = _base_manifest_kwargs(
            mode=TrainingMode.PRODUCTION,
            gpu_required=True,
            quality_policy_id="qp-production-v1",
            artifact_verification_required=True,
        )
        kwargs.pop("allow_cpu_fallback", None)
        with pytest.raises((ValidationError, ValueError)):
            TrainingManifest(**kwargs)

    def test_request_rejects_allow_cpu_fallback_true(self) -> None:
        with pytest.raises(ModeValidationError, match="cpu_fallback"):
            validate_mode(_production_request(extra={"allow_cpu_fallback": "1"}))

    def test_request_rejects_allow_cpu_fallback_missing(self) -> None:
        """A production request without allow_cpu_fallback in
        extra_constraints is accepted at the dispatch boundary: the
        validator only rejects an explicit ``allow_cpu_fallback=1``
        (absent is treated as 'not enabled' = OK). This documents the
        dispatch-layer semantics."""
        req = _production_request()
        ec = dict(req.extra_constraints)
        ec.pop("allow_cpu_fallback", None)
        req = req.model_copy(update={"extra_constraints": ec})
        # Absent allow_cpu_fallback is permissive (not "1") — no raise.
        assert validate_mode(req) == TrainingMode.PRODUCTION


# ---------------------------------------------------------------------------
# Production mode — quality policy required
# ---------------------------------------------------------------------------


class TestProductionQualityPolicyRequired:
    def test_manifest_rejects_missing_quality_policy_id(self) -> None:
        with pytest.raises((ValidationError, ValueError)):
            _production_manifest(quality_policy_id=None)

    def test_manifest_rejects_empty_quality_policy_id(self) -> None:
        with pytest.raises((ValidationError, ValueError)):
            _production_manifest(quality_policy_id="")

    def test_manifest_rejects_quality_policy_id_omitted(self) -> None:
        kwargs = _base_manifest_kwargs(
            mode=TrainingMode.PRODUCTION,
            gpu_required=True,
            allow_cpu_fallback=False,
            artifact_verification_required=True,
        )
        kwargs.pop("quality_policy_id", None)
        with pytest.raises((ValidationError, ValueError)):
            TrainingManifest(**kwargs)

    def test_request_rejects_missing_quality_policy_id(self) -> None:
        with pytest.raises(ModeValidationError, match="quality_policy_id"):
            validate_mode(_production_request(extra={"quality_policy_id": ""}))

    def test_request_rejects_quality_policy_id_omitted(self) -> None:
        req = _production_request()
        ec = dict(req.extra_constraints)
        ec.pop("quality_policy_id", None)
        req = req.model_copy(update={"extra_constraints": ec})
        with pytest.raises(ModeValidationError, match="quality_policy_id"):
            validate_mode(req)


# ---------------------------------------------------------------------------
# Production mode — artifact verification required
# ---------------------------------------------------------------------------


class TestProductionArtifactVerificationRequired:
    def test_manifest_rejects_artifact_verification_false(self) -> None:
        """Production mode requires artifact_verification_required=True at
        the manifest layer."""
        with pytest.raises((ValidationError, ValueError)):
            _production_manifest(artifact_verification_required=False)

    def test_manifest_rejects_artifact_verification_missing(self) -> None:
        """Omitting artifact_verification_required (default False) must
        fail in production."""
        kwargs = _base_manifest_kwargs(
            mode=TrainingMode.PRODUCTION,
            gpu_required=True,
            allow_cpu_fallback=False,
            quality_policy_id="qp-production-v1",
        )
        kwargs.pop("artifact_verification_required", None)
        with pytest.raises((ValidationError, ValueError)):
            TrainingManifest(**kwargs)

    def test_request_rejects_artifact_verification_not_set(self) -> None:
        """validate_mode rejects a production request whose
        artifact_verification_required is not set to 1 (dispatch-layer
        enforcement)."""
        with pytest.raises(ModeValidationError, match="artifact_verification"):
            validate_mode(_production_request(extra={"artifact_verification_required": "0"}))

    def test_request_rejects_artifact_verification_missing(self) -> None:
        """A production request without artifact_verification_required in
        extra_constraints must be rejected by validate_mode."""
        req = _production_request()
        ec = dict(req.extra_constraints)
        ec.pop("artifact_verification_required", None)
        req = req.model_copy(update={"extra_constraints": ec})
        with pytest.raises(ModeValidationError, match="artifact_verification"):
            validate_mode(req)


# ---------------------------------------------------------------------------
# Production mode — registered dataset required (reject raw CSV paths)
# ---------------------------------------------------------------------------


class TestProductionRegisteredDatasetRequired:
    def test_manifest_rejects_missing_dataset_registry_ref(self) -> None:
        """Production requires a non-empty dataset_registry_ref."""
        with pytest.raises((ValidationError, ValueError)):
            _production_manifest(dataset_registry_ref=None)

    def test_manifest_rejects_empty_dataset_registry_ref(self) -> None:
        with pytest.raises((ValidationError, ValueError)):
            _production_manifest(dataset_registry_ref="")

    def test_manifest_rejects_raw_csv_path(self) -> None:
        """A raw CSV file path is not a registered dataset reference."""
        with pytest.raises((ValidationError, ValueError)):
            _production_manifest(dataset_registry_ref="data/raw/prices.csv")

    def test_manifest_rejects_raw_parquet_path(self) -> None:
        """A raw parquet file path is not a registered dataset reference."""
        with pytest.raises((ValidationError, ValueError)):
            _production_manifest(dataset_registry_ref="/mnt/data/features.parquet")

    def test_manifest_rejects_file_uri(self) -> None:
        """A file:// URI is not a registered dataset reference."""
        with pytest.raises((ValidationError, ValueError)):
            _production_manifest(dataset_registry_ref="file:///workspace/data.csv")

    def test_manifest_accepts_registered_id(self) -> None:
        """An opaque registered dataset id is accepted in production."""
        m = _production_manifest(dataset_registry_ref="ds-registry-l3-001")
        assert m.dataset_registry_ref == "ds-registry-l3-001"

    def test_request_rejects_raw_csv_ref(self) -> None:
        """validate_mode rejects a production request whose
        dataset_manifest_ref is a raw CSV path."""
        with pytest.raises(ModeValidationError, match="registered dataset"):
            validate_mode(_production_request(dataset_manifest_ref="data/raw/prices.csv"))

    def test_request_accepts_registered_ref(self) -> None:
        """validate_mode accepts a production request with a registered
        dataset manifest ref (opaque id, not a raw path)."""
        req = _production_request(dataset_manifest_ref="ds-registry-l3-001:abc")
        assert validate_mode(req) == TrainingMode.PRODUCTION


# ---------------------------------------------------------------------------
# Production mode — any required field missing fails closed
# ---------------------------------------------------------------------------


class TestProductionFailsClosed:
    def test_manifest_with_all_defaults_rejected(self) -> None:
        """A production manifest that supplies NONE of the production-only
        fields must be rejected — fail closed."""
        with pytest.raises((ValidationError, ValueError)):
            TrainingManifest(**_base_manifest_kwargs(mode=TrainingMode.PRODUCTION))

    @pytest.mark.parametrize(
        "drop_field",
        [
            "gpu_required",
            "allow_cpu_fallback",
            "quality_policy_id",
            "artifact_verification_required",
            "dataset_registry_ref",
        ],
    )
    def test_manifest_rejects_when_any_required_field_missing(self, drop_field: str) -> None:
        kwargs = _base_manifest_kwargs(
            mode=TrainingMode.PRODUCTION,
            gpu_required=True,
            allow_cpu_fallback=False,
            quality_policy_id="qp-production-v1",
            artifact_verification_required=True,
            dataset_registry_ref="ds-registry-l3-001",
        )
        kwargs.pop(drop_field, None)
        with pytest.raises((ValidationError, ValueError)):
            TrainingManifest(**kwargs)

    def test_request_with_no_production_controls_rejected(self) -> None:
        """A production request carrying only the training_mode (no gpu /
        quality / artifact controls) must be rejected — fail closed."""
        req = _make_request(mode=TrainingMode.PRODUCTION)
        with pytest.raises(ModeValidationError, match="production mode validation failed"):
            validate_mode(req)


# ---------------------------------------------------------------------------
# Mode validation fails closed
# ---------------------------------------------------------------------------


class TestModeValidationFailsClosed:
    def test_unknown_mode_string_rejected_by_manifest(self) -> None:
        with pytest.raises((ValidationError, ValueError)):
            TrainingManifest(**_base_manifest_kwargs(mode="experimental"))  # type: ignore[arg-type]

    def test_unknown_mode_string_rejected_by_validate_mode(self) -> None:
        """validate_mode rejects an unknown training_mode string."""
        req = _make_request(extra={"training_mode": "experimental"})
        with pytest.raises(ModeValidationError, match="unknown training_mode"):
            validate_mode(req)

    def test_manifest_rejects_gpu_false_and_cpu_fallback_true_together(self) -> None:
        """Multiple production violations at once must still fail closed."""
        with pytest.raises((ValidationError, ValueError)):
            _production_manifest(gpu_required=False, allow_cpu_fallback=True)

    def test_manifest_rejects_all_constraints_violated(self) -> None:
        """Every production constraint violated simultaneously must fail."""
        with pytest.raises((ValidationError, ValueError)):
            _production_manifest(
                gpu_required=False,
                allow_cpu_fallback=True,
                quality_policy_id=None,
                artifact_verification_required=False,
                dataset_registry_ref="raw/data.csv",
            )

    def test_request_rejects_all_constraints_violated(self) -> None:
        """Every production constraint violated simultaneously must fail at
        the dispatch boundary too."""
        req = _production_request(
            dataset_manifest_ref="raw/data.csv",
            extra={
                "gpu_required": "0",
                "allow_cpu_fallback": "1",
                "quality_policy_id": "",
                "artifact_verification_required": "0",
            },
        )
        with pytest.raises(ModeValidationError, match="production mode validation failed"):
            validate_mode(req)

    def test_manifest_production_error_message_lists_all_violations(self) -> None:
        """The production rejection message should enumerate every unmet
        requirement so the operator can fix them in one pass (fail loud)."""
        with pytest.raises((ValidationError, ValueError)) as exc_info:
            _production_manifest(
                gpu_required=False,
                allow_cpu_fallback=True,
                quality_policy_id=None,
                artifact_verification_required=False,
                dataset_registry_ref=None,
            )
        msg = str(exc_info.value)
        assert "gpu_required" in msg
        assert "cpu_fallback" in msg
        assert "quality_policy_id" in msg
        assert "artifact_verification" in msg
        assert "dataset_registry_ref" in msg
