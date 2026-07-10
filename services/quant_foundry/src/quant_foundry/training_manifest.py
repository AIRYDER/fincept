"""
quant_foundry.training_manifest — Stage Task 1 dispatch envelope (TASK-0504 staging).

The ``TrainingManifest`` is the **operator-facing contract** for dispatching
a single baseline training job end-to-end. It packages:

- a feature-lake PIT manifest reference (``feature_lake_manifest_ref``)
- the model family and baseline hyperparameters (no secret values)
- walk-forward validation window splits (train / validation / test)
- purged-fold boundaries (forwarded from the feature-lake manifest)
- a budget envelope (``budget_cents`` + ``timeout_seconds``) that the
  ``BudgetGuard`` enforces before the job is dispatched
- reproducibility pins (random seed, optional hardware class label)
- a **training mode** (``mode``) that selects which rules apply to the
  job (see :class:`TrainingMode` and :data:`MODE_RULES` below)

The manifest is **schema-versioned** (``schema_version=1``) and frozen
(``extra='forbid'``) so a manifest can be hashed, signed, and referenced
by the dispatch script instead of being passed by raw dict.

Why a separate schema (rather than reusing ``RunPodTrainingRequest``):

- ``RunPodTrainingRequest`` is the **cross-boundary** schema that the
  worker (RunPod or mock) sees. It must NOT carry anything the worker
  doesn't need (e.g. budget envelope, walk-forward splits, report path).
- ``TrainingManifest`` is the **operator-facing** staging schema. It
  carries the additional context the gateway + dispatch script need to
  budget, split, and audit. The dispatch script then translates it into a
  ``RunPodTrainingRequest`` (or a local-trainer call) at the boundary.

This module is **file-disjoint from all active builders**: it imports
from ``dataset_manifest.py`` (TASK-0405) and ``schemas.py`` (TASK-0302)
read-only, and does NOT touch gateway, dossier, sentinel, tournament,
or outbox. The dispatch path lives in a sibling module.

Invariants (enforced + tested):
- ``schema_version == 1``.
- Frozen + ``extra='forbid'`` (audit integrity).
- ``train_window_ns`` and ``val_window_ns`` and ``test_window_ns`` are
  strictly positive.
- ``budget_cents >= 0``; ``timeout_seconds > 0`` unless the operator
  explicitly opts in with ``timeout_seconds == 0`` (immediate-timeout
  test path).
- ``model_family`` is one of the allowlist families the worker can
  train. We re-use the alpha_genome allowlist if it exists, else fall
  back to a small literal allowlist.
- ``hyperparameters`` may only contain keys the chosen ``model_family``
  supports, with values inside the allowed bounds.
- No secret-shaped values are accepted anywhere — the constructor
  rejects feature / hyperparameter names that look like credentials.
- **Production mode fails closed**: if ``mode == production`` and any of
  ``gpu_required``, ``allow_cpu_fallback == False``, a registered dataset
  reference, ``quality_policy_id``, and ``artifact_verification_required``
  are not satisfied, construction is rejected. Local CPU training is
  **not** an acceptance substitute for a production run.

.. note::

    **Local training is not an acceptance substitute.** A ``canary`` or
    ``research`` run may execute on the local CPU trainer for contract
    proofs, but a ``production`` run MUST execute on real RunPod GPU
    infrastructure with a registered L3/L4 dataset. The mode validation
    below enforces the *request* shape; the dispatch path is responsible
    for routing production jobs to GPU workers and rejecting CPU fallback.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from collections.abc import Mapping
from enum import StrEnum
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from fincept_core.datasets.cv import (
    derive_walk_forward_window as _canonical_derive_walk_forward_window,
)

# Quality-policy registry (Phase 3 / T-3.2). Imported lazily-safe: the
# quality_report module only imports from dataset_manifest (not from this
# module), so there is no circular import. The registry supplies the
# known ``quality_policy_id`` values that production-mode manifests must
# reference.
from quant_foundry.data_ingestion.quality_report import (
    QualityPolicy,
    resolve_quality_policy,
)

# Phase 8 / T-8.1: column roles live in dataset_manifest (file-disjoint).
# Imported read-only here so ModelTaskSpec can reference ColumnRoles for
# task-spec validation. dataset_manifest does NOT import from this module
# (it re-declares TrainingMode to stay file-disjoint), so there is no
# circular import.
from quant_foundry.dataset_manifest import (
    ColumnRoles,
    ColumnRolesValidationResult,
)

# Try to import the alpha_genome allowlists (preferred). Fall back to a
# small literal allowlist if the module is not yet importable (e.g. older
# deployment where the lab is not enabled).
try:
    from quant_foundry.alpha_genome import (
        ALLOWED_MODEL_FAMILIES as _AG_MODEL_FAMILIES,
    )
    from quant_foundry.alpha_genome import (
        HYPERPARAM_BOUNDS as _AG_HYPERPARAM_BOUNDS,
    )
    from quant_foundry.alpha_genome import (
        MODEL_FAMILY_REGISTRY as _AG_MODEL_FAMILY_REGISTRY,
    )
    from quant_foundry.alpha_genome import (
        FamilyValidationResult as _AGFamilyValidationResult,
    )

    _ALLOWED_MODEL_FAMILIES: frozenset[str] = _AG_MODEL_FAMILIES
    _HYPERPARAM_BOUNDS: dict[str, dict[str, tuple[float, float]]] = dict(_AG_HYPERPARAM_BOUNDS)
    _FAMILY_REGISTRY: Any = _AG_MODEL_FAMILY_REGISTRY
except Exception:
    _ALLOWED_MODEL_FAMILIES = frozenset({"gbm", "catboost", "logreg", "linear"})
    _HYPERPARAM_BOUNDS = {
        "gbm": {
            "n_estimators": (10.0, 5000.0),
            "max_depth": (2.0, 12.0),
            "learning_rate": (1e-4, 1.0),
            "min_child_samples": (1.0, 200.0),
        },
        "catboost": {
            "iterations": (10.0, 5000.0),
            "depth": (2.0, 10.0),
            "learning_rate": (1e-4, 1.0),
        },
        "logreg": {"C": (1e-4, 100.0), "max_iter": (50.0, 5000.0)},
        "linear": {"alpha": (1e-4, 10.0), "max_iter": (100.0, 10000.0)},
    }
    _FAMILY_REGISTRY = None
    _AGFamilyValidationResult = None  # type: ignore[assignment,misc]


class ModelFamily(StrEnum):
    """Allowlisted model families for baseline training."""

    GBM = "gbm"
    CATBOOST = "catboost"
    LOGREG = "logreg"
    LINEAR = "linear"


class ModelFamilyStr(str):
    """A ``str`` subclass that exposes ``.value`` for backward compat.

    Code throughout the dispatch path (e.g.
    ``local_training_dispatch.py``) calls ``manifest.model_family.value``
    expecting a ``ModelFamily`` enum. Since ``model_family`` now accepts
    arbitrary strings (research mode allows experimental families), we
    normalise the stored value to ``ModelFamilyStr`` so ``.value`` always
    works while the field remains a plain string for comparison and
    serialisation.
    """

    __slots__ = ()

    @property
    def value(self) -> str:
        return str.__str__(self)


class TrainingMode(StrEnum):
    """Training mode — selects which rules apply to a training job.

    The mode is the **single source of truth** for what invariants a job
    must satisfy. Builders and operators should consult
    :data:`MODE_RULES` (the mode rules table) when deciding what rules
    apply to a given mode.

    Members:
        CANARY: Small registered dataset, may use tiny artifacts, never
            promotion eligible by default. Used for contract proofs and
            smoke tests. CPU fallback allowed.
        RESEARCH: Real RunPod training, experimental model families
            allowed, promotion disabled unless explicitly escalated.
            CPU fallback allowed for local iteration.
        PRODUCTION: Registered L3/L4 dataset, GPU required, artifact
            verification required, quality gates required, no CPU
            fallback. Local training is **not** an acceptance substitute.
    """

    CANARY = "canary"
    RESEARCH = "research"
    PRODUCTION = "production"


# ---------------------------------------------------------------------------
# Mode rules table — the single source of truth for per-mode rules.
# ---------------------------------------------------------------------------
#
# Builders and operators consult this table when deciding what rules apply
# to a given mode. Each entry maps a :class:`TrainingMode` to the rules
# that mode enforces. The ``TrainingManifest`` validator and
# ``quant_foundry.runpod_training.validate_mode`` both reference these
# rules so there is exactly one place to look.
#
# Fields:
#   gpu_required               — must the job run on a GPU worker?
#   allow_cpu_fallback         — may the job fall back to a CPU trainer?
#   registered_dataset_required— must dataset_manifest_ref point at a
#                                 registered manifest (not a raw CSV)?
#   quality_policy_required    — must quality_policy_id be present?
#   artifact_verification_required — must the artifact be hash-verified?
#   promotion_eligible_default — is the resulting dossier promotion
#                                 eligible by default (before gates)?
#   description                — human-readable summary.
MODE_RULES: dict[TrainingMode, dict[str, object]] = {
    TrainingMode.CANARY: {
        "gpu_required": False,
        "allow_cpu_fallback": True,
        "registered_dataset_required": False,
        "quality_policy_required": False,
        "artifact_verification_required": False,
        "promotion_eligible_default": False,
        "description": (
            "Small registered dataset, may use tiny artifacts, never "
            "promotion eligible by default. Used for contract proofs "
            "and smoke tests."
        ),
    },
    TrainingMode.RESEARCH: {
        "gpu_required": False,
        "allow_cpu_fallback": True,
        "registered_dataset_required": False,
        "quality_policy_required": False,
        "artifact_verification_required": False,
        "promotion_eligible_default": False,
        "description": (
            "Real RunPod training, experimental model families allowed, "
            "promotion disabled unless explicitly escalated. CPU "
            "fallback allowed for local iteration."
        ),
    },
    TrainingMode.PRODUCTION: {
        "gpu_required": True,
        "allow_cpu_fallback": False,
        "registered_dataset_required": True,
        "quality_policy_required": True,
        "artifact_verification_required": True,
        "promotion_eligible_default": False,
        "description": (
            "Registered L3/L4 dataset, GPU required, artifact "
            "verification required, quality gates required, no CPU "
            "fallback. Local training is NOT an acceptance substitute."
        ),
    },
}


# ---------------------------------------------------------------------------
# Model family registry integration (Phase 7 / T-7.1)
# ---------------------------------------------------------------------------
#
# ``validate_family_for_mode`` is the production-tree-challenger gating
# hook. It consults the :data:`MODEL_FAMILY_REGISTRY` (the single source
# of truth for which families may run in production) and enforces:
#   - the family is registered (unknown family rejected),
#   - the family declares an artifact loader (no loader rejected),
#   - for production: the family maps to a GPU RunPod image OR carries an
#     explicit baseline exception,
#   - for canary/research: the GPU requirement is advisory (warning only).
#
# This is *additive* to the legacy ``_validate_mode_family`` allowlist
# check on ``TrainingManifest`` (which gates the lab's bounded mutation
# family set: gbm / catboost / logreg / linear). The registry uses the
# production deployment family ids (lightgbm_baseline, catboost_gpu,
# xgboost_gpu, logreg_sanity, linear_sanity). New production-tree
# challenger code (T-7.2 / T-7.3) calls this function with the registry
# family id; the legacy manifest path is unchanged.


def validate_family_for_mode(
    *,
    family_id: str,
    mode: TrainingMode | str,
    has_gpu: bool = False,
) -> _AGFamilyValidationResult | None:
    """Validate a registry family id against ``mode`` rules.

    Returns a :class:`FamilyValidationResult` (``passed`` True only when
    there are no errors). Returns ``None`` when the registry is not
    importable (older deployment) so callers can fall back gracefully.

    Args:
        family_id: the registry family id (e.g. ``"catboost_gpu"``).
        mode: a :class:`TrainingMode` or its string value.
        has_gpu: whether a GPU is available for this run. Advisory for
            canary/research; ignored for production (production requires
            the family to *map* to a GPU image, not merely have one
            available at dispatch time).
    """
    if _FAMILY_REGISTRY is None or _AGFamilyValidationResult is None:
        return None
    mode_str = mode.value if isinstance(mode, TrainingMode) else str(mode)
    return _FAMILY_REGISTRY.validate_family(  # type: ignore[no-any-return]  # _FAMILY_REGISTRY is Any when alpha_genome is importable
        family_id=family_id,
        mode=mode_str,
        has_gpu=has_gpu,
    )


def is_family_registered(family_id: str) -> bool:
    """Return True if ``family_id`` is registered in the family registry.

    Returns ``False`` when the registry is not importable.
    """
    if _FAMILY_REGISTRY is None:
        return False
    return cast("bool", _FAMILY_REGISTRY.is_registered(family_id))


def get_family_spec(family_id: str) -> Any:
    """Return the :class:`ModelFamilySpec` for ``family_id``.

    Raises ``KeyError`` if the family is not registered. Returns ``None``
    when the registry is not importable.
    """
    if _FAMILY_REGISTRY is None:
        return None
    return _FAMILY_REGISTRY.get(family_id)


# Substrings that suggest a dataset ref is a raw file rather than a
# registered manifest id. Used by the production-mode validator to reject
# raw CSV / parquet paths.
_RAW_DATASET_EXTENSIONS = (".csv", ".parquet", ".csv.gz", ".parquet.gz", ".feather")


def _is_raw_dataset_ref(ref: str) -> bool:
    """Return True if ``ref`` looks like a raw file path rather than a
    registered dataset/manifest id.

    A registered dataset reference is an opaque id (e.g. ``"ds-test"``)
    that the feature-lake / dataset registry resolves. A raw reference is
    a filesystem path (``/workspace/dataset.csv``), a ``file://`` URI, or
    any string ending in a known tabular file extension.
    """
    if not ref:
        return False
    low = ref.lower()
    if low.startswith("file://"):
        return True
    if low.startswith("inline://"):
        return True
    if any(low.endswith(ext) for ext in _RAW_DATASET_EXTENSIONS):
        return True
    # A registered id is an opaque slug without path separators. A path
    # containing ``/`` or ``\\`` is treated as a raw filesystem reference.
    if "/" in ref or "\\" in ref:
        return True
    return False


# Substrings that suggest a field is carrying a secret. Reject at the
# schema boundary so a malicious / accidental credential can never ride
# into a manifest.
_SECRET_SUBSTRINGS = (
    "password",
    "token",
    "secret",
    "api_key",
    "apikey",
    "credential",
    "private_key",
    "dsn",
    "connection_string",
)
_NAME_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9_.\-]{0,63}$")


def _looks_like_secret(s: str) -> bool:
    low = s.lower()
    return any(sub in low for sub in _SECRET_SUBSTRINGS)


class TrainingManifest(BaseModel):
    """Operator-facing manifest for staging one baseline training job.

    Frozen + extra='forbid' (audit integrity). ``content_hash`` is
    computed from the canonical content and is the immutability key for
    the dispatch script.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    manifest_id: str
    feature_lake_manifest_ref: str  # the dataset_id from FeatureLakeManifest
    feature_lake_manifest_hash: str  # the manifest_hash from FeatureLakeManifest
    # ``model_family`` accepts any string so research mode can use
    # experimental families outside the baseline allowlist. Canary and
    # production modes enforce the allowlist in ``_model_family_allowed``.
    model_family: str
    hyperparameters: dict[str, float] = Field(default_factory=dict)
    train_window_ns: int
    val_window_ns: int
    test_window_ns: int
    label_horizon_ns: int
    random_seed: int | None = None
    hardware_class: str | None = None
    walk_forward_enabled: bool = True
    budget_cents: int = 0
    timeout_seconds: int = 600
    operator_note: str = ""
    # --- training mode (Phase 0) ---------------------------------------
    # The mode selects which rules apply. See TrainingMode + MODE_RULES.
    # Defaults to CANARY (the most lenient mode: CPU fallback allowed,
    # no GPU/quality requirements). This preserves backward compat for
    # manifests created before modes existed.
    mode: TrainingMode = TrainingMode.CANARY
    # GPU / CPU fallback controls. Production mode REQUIRES
    # gpu_required=True and allow_cpu_fallback=False (enforced below).
    gpu_required: bool = False
    allow_cpu_fallback: bool = True
    # Quality-gate policy id. Production mode REQUIRES a non-empty value.
    quality_policy_id: str | None = None
    # Registered dataset registry reference (L3/L4 dataset id). Production
    # mode REQUIRES a non-empty value that is NOT a raw CSV/parquet path.
    dataset_registry_ref: str | None = None
    # Whether the resulting dossier is promotion eligible. Defaults to
    # False for all modes (canary and research are never promotion
    # eligible by default; production must pass quality gates first).
    promotion_eligible: bool = False
    # Whether the resulting artifact must be hash-verified on import.
    # Production mode sets this via the dispatch path; the manifest
    # validator does not enforce it (the tests do not require it here).
    artifact_verification_required: bool = False
    content_hash: str = ""

    # --- validators -----------------------------------------------------

    @field_validator("manifest_id")
    @classmethod
    def _manifest_id_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("manifest_id must be non-empty")
        return v

    @field_validator("feature_lake_manifest_ref")
    @classmethod
    def _ref_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("feature_lake_manifest_ref must be non-empty")
        return v

    @field_validator("feature_lake_manifest_hash")
    @classmethod
    def _hash_shape(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("feature_lake_manifest_hash must be non-empty")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", v):
            raise ValueError("feature_lake_manifest_hash must be a 64-char hex SHA-256")
        return v.lower()

    @field_validator("model_family")
    @classmethod
    def _model_family_allowed(cls, v: Any) -> ModelFamilyStr:
        # Accept ModelFamily enum or plain string. Normalise to
        # ModelFamilyStr so downstream code that calls ``.value`` (e.g.
        # local_training_dispatch.py) keeps working, while the field
        # remains a plain string for comparison and serialisation.
        if isinstance(v, ModelFamily):
            v = v.value
        if not isinstance(v, str) or not v.strip():
            raise ValueError("model_family must be a non-empty string")
        # The cross-field allowlist check (mode-dependent) runs in
        # ``_validate_mode_family`` after the mode field is available.
        return ModelFamilyStr(v)

    @field_validator("hyperparameters")
    @classmethod
    def _hyperparams_safe(cls, v: Mapping[str, float], info: Any) -> dict[str, float]:
        # We don't have the model_family yet in this validator, so we do
        # the cross-field check in ``_validate_hyperparams_for_family``.
        for k in v:
            if not isinstance(k, str) or not k:
                raise ValueError("hyperparameter names must be non-empty strings")
            if _looks_like_secret(k):
                raise ValueError(f"hyperparameter name {k!r} looks like a secret")
            if not _NAME_PATTERN.match(k):
                raise ValueError(f"hyperparameter name {k!r} must match {_NAME_PATTERN.pattern}")
        return dict(v)

    @model_validator(mode="after")
    def _validate_mode_family(self) -> TrainingManifest:
        """Mode-dependent model_family allowlist check.

        - CANARY and PRODUCTION: ``model_family`` must be in the baseline
          allowlist (no experimental families).
        - RESEARCH: any non-empty family string is allowed (experimental
          model families are permitted).
        """
        if self.mode != TrainingMode.RESEARCH:
            if self.model_family not in _ALLOWED_MODEL_FAMILIES:
                raise ValueError(
                    f"model_family {self.model_family!r} not in allowlist; "
                    f"allowed: {sorted(_ALLOWED_MODEL_FAMILIES)}"
                )
        return self

    @model_validator(mode="after")
    def _validate_hyperparams_for_family(self) -> TrainingManifest:
        bounds = _HYPERPARAM_BOUNDS.get(self.model_family, {})
        # If the family has no defined bounds (e.g. an experimental
        # family in research mode), skip the hyperparameter bounds check
        # — we cannot bound what we have not profiled.
        if not bounds:
            return self
        bad_keys = set(self.hyperparameters) - set(bounds)
        if bad_keys:
            raise ValueError(
                f"hyperparameters {sorted(bad_keys)!r} not defined for "
                f"model_family {self.model_family!r}; "
                f"allowed: {sorted(bounds)}"
            )
        for k, val in self.hyperparameters.items():
            lo, hi = bounds[k]
            if not (lo <= float(val) <= hi):
                raise ValueError(f"hyperparameter {k}={val} outside bounds [{lo}, {hi}]")
        return self

    @model_validator(mode="after")
    def _windows_positive(self) -> TrainingManifest:
        for name in ("train_window_ns", "val_window_ns", "test_window_ns", "label_horizon_ns"):
            v = getattr(self, name)
            if v <= 0:
                raise ValueError(f"{name} must be > 0; got {v}")
        if self.budget_cents < 0:
            raise ValueError(f"budget_cents must be >= 0; got {self.budget_cents}")
        if self.timeout_seconds < 0:
            raise ValueError(f"timeout_seconds must be >= 0; got {self.timeout_seconds}")
        return self

    @model_validator(mode="after")
    def _operator_note_safe(self) -> TrainingManifest:
        if self.operator_note and _looks_like_secret(self.operator_note):
            raise ValueError("operator_note contains a secret-like substring; reject")
        return self

    @model_validator(mode="after")
    def _validate_mode_rules(self) -> TrainingManifest:
        """Enforce per-mode rules from :data:`MODE_RULES`.

        Production mode fails closed: if any production requirement is
        missing or contradicted, construction is rejected. Canary and
        research modes are permissive (they allow CPU fallback and do
        not require a quality policy, artifact verification, or
        registered dataset).

        Local training is **not** an acceptance substitute for a
        production run — this validator enforces the request shape; the
        dispatch path enforces routing to a GPU worker.
        """
        errors: list[str] = []

        if self.mode == TrainingMode.PRODUCTION:
            # gpu_required must be True.
            if not self.gpu_required:
                errors.append(
                    "production mode requires gpu_required=True "
                    "(local CPU training is not an acceptance substitute)"
                )
            # allow_cpu_fallback must be False.
            if self.allow_cpu_fallback:
                errors.append(
                    "production mode requires allow_cpu_fallback=False "
                    "(no CPU fallback for production runs)"
                )
            # quality_policy_id must be present and non-empty.
            if not self.quality_policy_id or not self.quality_policy_id.strip():
                errors.append(
                    "production mode requires a non-empty quality_policy_id "
                    "(quality gates are mandatory)"
                )
            # artifact_verification_required must be True.
            if not self.artifact_verification_required:
                errors.append(
                    "production mode requires artifact_verification_required=True "
                    "(artifact hash verification is mandatory)"
                )
            # dataset_registry_ref must be present, non-empty, and a
            # registered dataset id (not a raw CSV/parquet path or file URI).
            if not self.dataset_registry_ref or not self.dataset_registry_ref.strip():
                errors.append(
                    "production mode requires a non-empty dataset_registry_ref "
                    "(registered L3/L4 dataset id)"
                )
            elif _is_raw_dataset_ref(self.dataset_registry_ref):
                errors.append(
                    "production mode requires a registered dataset "
                    "reference (L3/L4 manifest id), not a raw CSV/path: "
                    f"{self.dataset_registry_ref!r}"
                )

        if errors:
            # Join all errors so the operator sees every unmet requirement
            # in a single rejection (fail closed, fail loud).
            raise ValueError("production mode validation failed: " + "; ".join(errors))
        return self

    @model_validator(mode="after")
    def _validate_quality_policy_id_known(self) -> TrainingManifest:
        """Validate that ``quality_policy_id`` names a real policy.

        When a ``quality_policy_id`` is provided (non-empty), it must
        resolve to a registered :class:`QualityPolicy` in the
        :data:`QUALITY_POLICY_REGISTRY`. This is fail closed: an unknown
        policy id is rejected at construction so a manifest can never
        silently reference a non-existent gate. Production mode already
        requires a non-empty id (see :meth:`_validate_mode_rules`); this
        validator additionally confirms the id is real.

        A ``None`` or empty id is left to the mode rules (canary/research
        permit no policy; production requires one) — this validator only
        rejects *unknown* ids, not *missing* ones.
        """
        if self.quality_policy_id and self.quality_policy_id.strip():
            if resolve_quality_policy(self.quality_policy_id) is None:
                raise ValueError(
                    "quality_policy_id "
                    f"{self.quality_policy_id!r} is not a registered "
                    "quality policy; known ids: "
                    f"{sorted(_known_quality_policy_ids())}"
                )
        return self

    def model_post_init(self, __context: Any) -> None:
        ch = _compute_content_hash(self)
        object.__setattr__(self, "content_hash", ch)

    def model_copy(  # type: ignore[override]
        self,
        *,
        update: dict[str, Any] | None = None,
        deep: bool = False,
    ) -> TrainingManifest:
        copied = super().model_copy(update=update, deep=deep)
        ch = _compute_content_hash(copied)
        object.__setattr__(copied, "content_hash", ch)
        return copied

    # --- helpers --------------------------------------------------------

    def resolve_quality_policy(self) -> QualityPolicy | None:
        """Resolve this manifest's ``quality_policy_id`` to a policy.

        Returns the registered :class:`QualityPolicy` for the manifest's
        ``quality_policy_id``, or ``None`` when no id is set or the id is
        unknown. Construction already rejects unknown ids (when
        provided), so for a successfully-constructed manifest this
        returns the policy whenever ``quality_policy_id`` is set.
        """
        if not self.quality_policy_id:
            return None
        return resolve_quality_policy(self.quality_policy_id)

    def to_dispatch_request(
        self,
        *,
        job_id: str,
    ) -> dict[str, Any]:
        """Translate this manifest into a ``RunPodTrainingRequest``-shaped dict.

        The dispatch script then hands this dict to the trainer (local or
        remote). ``dataset_manifest_ref`` carries the feature_lake id +
        hash so the worker can verify what it is training on.

        The training mode and GPU/CPU-fallback controls are forwarded via
        ``extra_constraints`` so the worker (which uses the
        ``RunPodTrainingRequest`` schema) can enforce them without a
        schema change to the cross-boundary contract.
        """
        return {
            "schema_version": 1,
            "job_id": job_id,
            "dataset_manifest_ref": (
                f"{self.feature_lake_manifest_ref}:{self.feature_lake_manifest_hash[:16]}"
            ),
            "model_family": self.model_family,
            "search_space": {},  # operator pinned values; no search for baseline
            "random_seed": self.random_seed,
            "hardware_class": self.hardware_class,
            "extra_constraints": {
                "train_window_ns": str(self.train_window_ns),
                "val_window_ns": str(self.val_window_ns),
                "test_window_ns": str(self.test_window_ns),
                "label_horizon_ns": str(self.label_horizon_ns),
                "walk_forward_enabled": "1" if self.walk_forward_enabled else "0",
                "manifest_content_hash": self.content_hash,
                # Phase 0: forward the training mode + GPU controls so the
                # worker / dispatch path can enforce them.
                "training_mode": self.mode.value,
                "gpu_required": "1" if self.gpu_required else "0",
                "allow_cpu_fallback": "1" if self.allow_cpu_fallback else "0",
                "artifact_verification_required": (
                    "1" if self.artifact_verification_required else "0"
                ),
                "promotion_eligible": "1" if self.promotion_eligible else "0",
                **({"quality_policy_id": self.quality_policy_id} if self.quality_policy_id else {}),
                **(
                    {"dataset_registry_ref": self.dataset_registry_ref}
                    if self.dataset_registry_ref
                    else {}
                ),
            },
        }


def _known_quality_policy_ids() -> frozenset[str]:
    """Return the set of registered quality-policy ids.

    Used by the manifest validator's error message so an operator can
    see the valid options when an unknown id is rejected.
    """
    from quant_foundry.data_ingestion.quality_report import (
        QUALITY_POLICY_REGISTRY,
    )

    return QUALITY_POLICY_REGISTRY.known_ids()


def _compute_content_hash(manifest: TrainingManifest) -> str:
    """Deterministic SHA-256 over the canonical content (excluding content_hash)."""
    payload = {
        "schema_version": manifest.schema_version,
        "manifest_id": manifest.manifest_id,
        "feature_lake_manifest_ref": manifest.feature_lake_manifest_ref,
        "feature_lake_manifest_hash": manifest.feature_lake_manifest_hash,
        "model_family": manifest.model_family,
        "hyperparameters": dict(sorted(manifest.hyperparameters.items())),
        "train_window_ns": manifest.train_window_ns,
        "val_window_ns": manifest.val_window_ns,
        "test_window_ns": manifest.test_window_ns,
        "label_horizon_ns": manifest.label_horizon_ns,
        "random_seed": manifest.random_seed,
        "hardware_class": manifest.hardware_class,
        "walk_forward_enabled": manifest.walk_forward_enabled,
        "budget_cents": manifest.budget_cents,
        "timeout_seconds": manifest.timeout_seconds,
        "operator_note": manifest.operator_note,
        # Phase 0: include the training mode + GPU/quality controls so a
        # content-hash change reflects a mode change (audit integrity).
        "mode": manifest.mode.value,
        "gpu_required": manifest.gpu_required,
        "allow_cpu_fallback": manifest.allow_cpu_fallback,
        "quality_policy_id": manifest.quality_policy_id,
        "dataset_registry_ref": manifest.dataset_registry_ref,
        "promotion_eligible": manifest.promotion_eligible,
        "artifact_verification_required": manifest.artifact_verification_required,
    }
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


# ---------------------------------------------------------------------------
# Walk-forward split
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class WalkForwardWindow:
    """A single (train, val, test) triple in nanoseconds since epoch.

    Boundaries are inclusive of start, exclusive of end. The three
    windows do not overlap; ``train_end <= val_start <= val_end <= test_start``.
    The label horizon must be shorter than the gap between train_end and
    val_start (and val_end and test_start) so the label window of a train
    row does not bleed into validation or test.
    """

    train_start: int
    train_end: int
    val_start: int
    val_end: int
    test_start: int
    test_end: int
    label_horizon_ns: int

    def to_dict(self) -> dict[str, int]:
        return {
            "train_start": self.train_start,
            "train_end": self.train_end,
            "val_start": self.val_start,
            "val_end": self.val_end,
            "test_start": self.test_start,
            "test_end": self.test_end,
            "label_horizon_ns": self.label_horizon_ns,
        }


def derive_walk_forward_window(
    *,
    train_window_ns: int,
    val_window_ns: int,
    test_window_ns: int,
    label_horizon_ns: int,
    as_of_ts: int,
) -> WalkForwardWindow:
    """Derive a single (train, val, test) triple ending at ``as_of_ts``.

    Layout (oldest → newest):
        [train_start  train_end][gap = label_horizon][val_start  val_end]
        [gap = label_horizon][test_start  test_end == as_of_ts]

    The label horizon acts as an embargo between consecutive windows so a
    training row's label does not overlap validation or test.

    This is a thin wrapper around the canonical
    ``fincept_core.datasets.cv.derive_walk_forward_window`` so the
    quant_foundry manifest builder and the rest of the platform agree on
    the walk-forward window math. The canonical result is re-wrapped into
    the local :class:`WalkForwardWindow` dataclass to preserve the
    public ``quant_foundry.training_manifest`` import surface.
    """
    canonical = _canonical_derive_walk_forward_window(
        train_window_ns=train_window_ns,
        val_window_ns=val_window_ns,
        test_window_ns=test_window_ns,
        label_horizon_ns=label_horizon_ns,
        as_of_ts=as_of_ts,
    )
    return WalkForwardWindow(
        train_start=canonical.train_start,
        train_end=canonical.train_end,
        val_start=canonical.val_start,
        val_end=canonical.val_end,
        test_start=canonical.test_start,
        test_end=canonical.test_end,
        label_horizon_ns=canonical.label_horizon_ns,
    )


# ---------------------------------------------------------------------------
# Phase 8 / T-8.1 — Model Task Spec (column roles, groups, weights, horizons)
# ---------------------------------------------------------------------------
#
# ``ModelTaskSpec`` is the **explicit** declaration of the learning task a
# training run performs. It binds a task type (binary / regression /
# ranking / multiclass) to the column roles declared in
# :class:`ColumnRoles` (dataset_manifest) and enforces:
# - the label column is declared in ``ColumnRoles.label_columns``,
# - ranking tasks require a group column (fail-closed),
# - the calibration policy is one of the allowed values,
# - weight / group columns (if set) exist in the column roles.
#
# This replaces the implicit "the trainer knows it's a binary task because
# the label is 0/1" pattern with an explicit, auditable task declaration.


# Allowed task types. Kept as a frozenset (not a StrEnum) so research mode
# can experiment with novel task types without a schema change — but the
# ranking-specific group-column check is always enforced.
_ALLOWED_TASK_TYPES: frozenset[str] = frozenset(
    {"binary", "regression", "ranking", "multiclass"},
)

# Allowed calibration policies. ``none`` = no calibration, ``platt`` =
# Platt scaling (logistic), ``isotonic`` = isotonic regression.
_ALLOWED_CALIBRATION_POLICIES: frozenset[str] = frozenset(
    {"none", "platt", "isotonic"},
)


class ModelTaskSpec(BaseModel):
    """Explicit declaration of the learning task for a training run.

    Frozen + ``extra='forbid'`` (audit integrity).

    Fields:
        task_type: the learning task type. One of ``"binary"``,
            ``"regression"``, ``"ranking"``, ``"multiclass"``.
        label_column: the target label column name. Must be non-empty and
            must appear in :attr:`ColumnRoles.label_columns` (validated by
            :func:`validate_task_spec`).
        horizon: the prediction horizon in bars/days. ``None`` when not
            applicable (e.g. a binary same-bar classifier).
        weight_column: the sample-weight column name. Optional. If set,
            must exist in the column roles.
        group_column: the group-id column name. **Required for ranking
            tasks** (fail-closed if missing). Optional for other task
            types.
        calibration_policy: the post-training calibration policy. One of
            ``"none"``, ``"platt"``, ``"isotonic"``. Defaults to ``"none"``.
        barrier_config: Tier 2.3 — triple-barrier labeling configuration.
            When set, the trainer knows the labels were produced by
            triple-barrier labeling (López de Prado Ch. 3) and records
            the barrier widths in the dossier metadata for auditability.
            The dict mirrors :class:`fincept_core.datasets.BarrierConfig`
            (profit_take_width, stop_loss_width, horizon_bars,
            min_volatility). Optional — absent for non-barrier tasks.
        meta_label_config: Tier 2.3b — meta-labeling configuration.
            When set, the trainer trains a secondary binary classifier
            (the "meta-model") that decides *whether to act* on the
            primary model's signal. The dict mirrors
            :class:`fincept_core.datasets.MetaLabelConfig`
            (side_column, label_column, meta_label_column). The primary
            model must be multiclass (triple-barrier). The meta-model
            is trained on the out-of-fold primary predictions (as an
            additional feature) → binary meta-label {0, 1}. Optional —
            absent when meta-labeling is not used.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    task_type: str
    label_column: str
    horizon: int | None = None
    weight_column: str | None = None
    group_column: str | None = None
    calibration_policy: str = "none"
    # Tier 2.3: triple-barrier label config (optional).
    barrier_config: dict[str, Any] | None = None
    # Tier 2.3b: meta-labeling config (optional).
    meta_label_config: dict[str, Any] | None = None

    @field_validator("task_type")
    @classmethod
    def _task_type_allowed(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("task_type must be a non-empty string")
        if v not in _ALLOWED_TASK_TYPES:
            raise ValueError(
                f"task_type {v!r} is not allowed; allowed: {sorted(_ALLOWED_TASK_TYPES)}"
            )
        return v

    @field_validator("label_column")
    @classmethod
    def _label_column_nonempty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("label_column must be a non-empty string")
        return v

    @field_validator("calibration_policy")
    @classmethod
    def _calibration_policy_allowed(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("calibration_policy must be a non-empty string")
        if v not in _ALLOWED_CALIBRATION_POLICIES:
            raise ValueError(
                f"calibration_policy {v!r} is not allowed; "
                f"allowed: {sorted(_ALLOWED_CALIBRATION_POLICIES)}"
            )
        return v

    @field_validator("horizon")
    @classmethod
    def _horizon_positive(cls, v: int | None) -> int | None:
        if v is not None and v <= 0:
            raise ValueError(f"horizon must be > 0 when set; got {v}")
        return v

    @model_validator(mode="after")
    def _ranking_requires_group(self) -> ModelTaskSpec:
        """Ranking tasks MUST declare a group column (fail-closed).

        A ranking task without a group id cannot be trained — LightGBM's
        lambdarank objective requires a group array. Fail closed at
        construction so the error surfaces before any data is loaded.
        """
        if self.task_type == "ranking" and not self.group_column:
            raise ValueError(
                "ranking task_type requires a non-empty group_column "
                "(a ranking request without a group id fails — "
                "LightGBM lambdarank needs a group array)"
            )
        return self

    @model_validator(mode="after")
    def _meta_label_requires_barrier(self) -> ModelTaskSpec:
        """Meta-labeling (Tier 2.3b) requires triple-barrier labels.

        The meta-model learns *when to act* on the primary model's
        directional signal. Without triple-barrier labels there is no
        meaningful "was the primary signal correct?" target — the
        barrier labels ARE the ground truth for the meta-label
        computation. Fail closed at construction.
        """
        if self.meta_label_config is not None and self.barrier_config is None:
            raise ValueError(
                "meta_label_config requires barrier_config — the "
                "meta-label is computed from the triple-barrier label "
                "and the primary model's signal (AFML Ch. 3.6)"
            )
        if self.meta_label_config is not None and self.task_type != "multiclass":
            raise ValueError(
                "meta_label_config requires task_type='multiclass' — "
                "the primary model must be a triple-barrier classifier"
            )
        return self


def validate_task_spec(
    spec: ModelTaskSpec,
    column_roles: ColumnRoles,
) -> ColumnRolesValidationResult:
    """Validate a :class:`ModelTaskSpec` against :class:`ColumnRoles`.

    Checks:
    - ``spec.label_column`` exists in ``column_roles.label_columns``.
    - ``spec.weight_column`` (if set) exists in the column roles' declared
      columns.
    - ``spec.group_column`` (if set) exists in the column roles' declared
      columns.
    - For ranking: ``spec.group_column`` must be set (already enforced at
      :class:`ModelTaskSpec` construction, re-checked here for defence in
      depth) and must match ``column_roles.group_column``.

    Args:
        spec: the :class:`ModelTaskSpec` to validate.
        column_roles: the :class:`ColumnRoles` the dataset declares.

    Returns:
        A :class:`ColumnRolesValidationResult`. ``passed`` is True only
        when there are no errors.
    """
    errors: list[str] = []
    warnings: list[str] = []

    available = column_roles.all_declared_columns

    # Label column must be declared as a label in the column roles.
    if spec.label_column not in set(column_roles.label_columns):
        errors.append(
            f"task_spec.label_column {spec.label_column!r} is not declared "
            f"in column_roles.label_columns "
            f"{list(column_roles.label_columns)!r}"
        )

    # Weight column (if set) must exist in the declared columns.
    if spec.weight_column is not None:
        if spec.weight_column not in available:
            errors.append(
                f"task_spec.weight_column {spec.weight_column!r} not found "
                "in column_roles declared columns"
            )
        elif (
            column_roles.weight_column is not None
            and spec.weight_column != column_roles.weight_column
        ):
            warnings.append(
                f"task_spec.weight_column {spec.weight_column!r} differs "
                f"from column_roles.weight_column "
                f"{column_roles.weight_column!r}"
            )

    # Group column (if set) must exist in the declared columns.
    if spec.group_column is not None:
        if spec.group_column not in available:
            errors.append(
                f"task_spec.group_column {spec.group_column!r} not found "
                "in column_roles declared columns"
            )
        elif (
            column_roles.group_column is not None and spec.group_column != column_roles.group_column
        ):
            warnings.append(
                f"task_spec.group_column {spec.group_column!r} differs "
                f"from column_roles.group_column "
                f"{column_roles.group_column!r}"
            )

    # Ranking: group column must be set (defence in depth — already
    # enforced at ModelTaskSpec construction).
    if spec.task_type == "ranking":
        if not spec.group_column:
            errors.append(
                "ranking task_type requires a non-empty group_column "
                "(ranking request without group id fails)"
            )
        elif column_roles.group_column is None:
            errors.append(
                "ranking task_type requires column_roles.group_column to "
                "be set (the group-id column must be declared in the "
                "dataset's column roles)"
            )

    passed = len(errors) == 0
    return ColumnRolesValidationResult(
        passed=passed,
        errors=tuple(errors),
        warnings=tuple(warnings),
    )


__all__ = [
    "MODE_RULES",
    "ModelFamily",
    "ModelFamilyStr",
    "ModelTaskSpec",
    "QualityPolicy",
    "TrainingManifest",
    "TrainingMode",
    "WalkForwardWindow",
    "derive_walk_forward_window",
    "get_family_spec",
    "is_family_registered",
    "resolve_quality_policy",
    "validate_family_for_mode",
    "validate_task_spec",
]
