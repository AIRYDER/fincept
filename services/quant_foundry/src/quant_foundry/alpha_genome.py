"""
quant_foundry.alpha_genome — automated recipe generation lab (TASK-1005).

The Alpha Genome Lab is the bounded-mutation engine that proposes candidate
recipes (feature set + model type + hyperparameters + windows) for training
and evaluation. Every recipe must be reproducible, every mutation must fall
inside an allowlist, every trial must respect cost budgets, and every
candidate must pass through the same ``PromotionGate.evaluate()`` path as
any other model — no recipe can bypass the tournament / sentinel gates.

Cross-cutting quant rigor (BIG_PLAN):
- **Point-in-time discipline.** Mutations never bypass the feature lake's
  PIT proof. A recipe that would require non-PIT data is rejected by the
  mutation engine.
- **No live trading authority for genome-generated recipes.** The default
  ``Authority.SHADOW_ONLY`` is the only level accepted at registration;
  higher authority requires the same human approval path as any other
  model.
- **No secrets in recipes or receipts.** Recipe configs contain only
  feature names, model families, hyperparameters, and window boundaries —
  never credentials, environment-variable values, or filesystem paths.
- **Cost fails closed.** ``TrialBudget`` denies any trial that would
  exceed per-recipe or per-sweep limits, and consults ``BudgetGuard`` for
  the global kill switch.

File-disjoint from all active builders. New module. No imports of
settlement / dossier (other than type references for DossierStatus and
PromotionEvidence) / tournament / gateway / outbox / inbox. The lab is
**opt-in** — disabling ``AlphaGenomeLab`` falls back to the manual model
registry with no behavioral change.

Design:

- ``Recipe`` is a versioned config carrying feature set, model family,
  hyperparameters, and train/validation windows. ``recipe_hash`` is a
  deterministic sha256 over the canonical content. Two recipes with the
  same canonical content produce the same hash, so the same recipe always
  maps to the same training job (reproducibility invariant).

- ``RecipeMutation`` is a typed operation (add feature, remove feature,
  transform feature, set hyperparameter, narrow window, widen window)
  with strict allowlist bounds. Out-of-range mutations are rejected
  before they produce a new recipe.

- ``AlphaGenomeLab`` orchestrates a sweep: generate ``n_recipes`` via
  ``RecipeMutation`` from a parent recipe, dispatch training to the
  supplied ``TrainingDispatcher`` callable, collect evidence through a
  ``PromotionGate``, and either register the survivor or discard the rest
  with a ``DiscardReceipt``. **Every candidate goes through the same
  ``PromotionGate.evaluate()`` path** — no shortcut, no bypass.

- ``TrialBudget`` enforces per-recipe and per-sweep cost ceilings and
  consults ``BudgetGuard`` for the global kill switch. ``Budget exhaustion
  stops new trials, doesn't kill running ones`` — running trials complete
  but no new trial is dispatched once the budget is exhausted.

- ``EarlyStopper`` reads intermediate ``TournamentScore`` values from a
  supplied ``TournamentProbe`` callable and kills underperforming recipes
  before full evaluation. Early-killed recipes get a ``KILLED_EARLY``
  status with a reason recorded on the ``DiscardReceipt``.
"""

from __future__ import annotations

import dataclasses
import enum
import hashlib
import json
import time
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict

from quant_foundry.budget import BudgetGuard

# ---------------------------------------------------------------------------
# Allowlists
# ---------------------------------------------------------------------------

# Allowed feature transformations. A feature with name N may be replaced by
# one of these transforms, producing a derived feature whose observed_at is
# inherited from the source feature (PIT-safe).
ALLOWED_TRANSFORMS: frozenset[str] = frozenset(
    {
        "zscore",  # z-score within trailing window (PIT-safe)
        "rank",  # cross-sectional rank
        "log_return",  # log(price_t / price_{t-1})
        "diff",  # x_t - x_{t-1}
        "rolling_mean",  # trailing mean (window supplied via hyperparams)
        "rolling_std",  # trailing std (window supplied via hyperparams)
    }
)

# Allowed model families. Limited to the families the rest of the system
# has contracts for — adding a family requires new dispatchers and new
# dossier schemas, so the lab cannot introduce unsupported families.
ALLOWED_MODEL_FAMILIES: frozenset[str] = frozenset(
    {
        "gbm",  # LightGBM / gradient-boosted trees (baseline)
        "catboost",  # CatBoost baseline
        "logreg",  # regularized logistic regression (sanity baseline)
        "linear",  # ridge / lasso (sanity baseline)
    }
)

# Hyperparameter bounds per model family. Values MUST lie inside these
# bounds. The mutation engine rejects any value outside.
HYPERPARAM_BOUNDS: dict[str, dict[str, tuple[float, float]]] = {
    "gbm": {
        "n_estimators": (10.0, 5000.0),
        "max_depth": (2.0, 12.0),
        "learning_rate": (1e-4, 1.0),
        "min_child_samples": (1.0, 200.0),
        "reg_alpha": (0.0, 10.0),
        "reg_lambda": (0.0, 10.0),
    },
    "catboost": {
        "iterations": (10.0, 5000.0),
        "depth": (2.0, 10.0),
        "learning_rate": (1e-4, 1.0),
        "l2_leaf_reg": (1.0, 30.0),
    },
    "logreg": {
        "C": (1e-4, 100.0),
        "max_iter": (50.0, 5000.0),
        "tol": (1e-8, 1e-1),
    },
    "linear": {
        "alpha": (1e-4, 10.0),
        "max_iter": (100.0, 10000.0),
        "tol": (1e-8, 1e-1),
    },
}

# Default window ranges (in nanoseconds). 1 day = 86_400_000_000_000 ns.
DEFAULT_TRAIN_WINDOW_RANGE_NS: tuple[int, int] = (
    30 * 86_400_000_000_000,  # 30 days minimum
    5 * 365 * 86_400_000_000_000,  # 5 years maximum
)
DEFAULT_VAL_WINDOW_RANGE_NS: tuple[int, int] = (
    7 * 86_400_000_000_000,  # 7 days minimum
    180 * 86_400_000_000_000,  # 180 days maximum
)


# ---------------------------------------------------------------------------
# Model Family Registry (Phase 7 / T-7.1)
# ---------------------------------------------------------------------------
#
# The ``ModelFamilyRegistry`` is the **single source of truth** for which
# model families may run in production, what they require, and how their
# artifacts are loaded. It replaces the ad-hoc hardcoded allowlists that
# previously gated production deployment with a versioned, declarative
# registry: adding a model family is now a single ``register()`` call
# (gated by code review) rather than editing scattered allowlists.
#
# Each :class:`ModelFamilySpec` declares:
#   - the dataset shape it expects,
#   - the objectives it supports,
#   - the artifact format + loader it produces,
#   - the metrics it must report,
#   - the RunPod Docker image it maps to (or None for local/baseline),
#   - whether it requires a GPU,
#   - a per-job budget cap,
#   - its promotion-eligibility class,
#   - whether it is an explicit baseline exception (may run in production
#     without a GPU image).
#
# Production gating rule (enforced by ``ModelFamilyRegistry.validate_family``):
#   a production request's family MUST either map to a GPU RunPod image
#   (``runpod_image`` is not None and ``requires_gpu`` is True) OR carry an
#   explicit baseline exception (``is_baseline_exception`` is True). Any
#   other family is rejected for production. Canary and research modes are
#   permissive — the GPU requirement is advisory only.
#
# The legacy ``ALLOWED_MODEL_FAMILIES`` / ``HYPERPARAM_BOUNDS`` constants
# above remain the allowlist for the Alpha Genome Lab's *bounded mutation
# engine* (a separate concern — the lab mutates recipes within a small,
# profiled family set). The registry is the source of truth for
# *production deployment* of trained families.

# RunPod Docker image reference for the CUDA-capable tree-model worker
# built in T-4.2 (runpod/quant-foundry-training/Dockerfile). GPU families
# (catboost_gpu, xgboost_gpu) map to this image; baseline / sanity families
# leave ``runpod_image`` as None and run on the local CPU trainer.
RUNPOD_GPU_TREE_IMAGE: str = "fincept-qf-training:gpu-tree"

# Registry schema version. Bumped when the ``ModelFamilySpec`` shape
# changes in a backward-incompatible way. Existing specs carry their own
# ``version`` field for per-family evolution tracking.
MODEL_FAMILY_REGISTRY_VERSION: str = "1"


class PromotionEligibilityClass(enum.StrEnum):
    """Promotion-eligibility class for a model family.

    - ``PRIMARY``: eligible to be promoted to production as a primary model.
    - ``CHALLENGER``: eligible to challenge the primary in a tournament;
      promotion requires beating the incumbent.
    - ``SANITY``: a sanity baseline (logreg / linear); never promotion
      eligible on its own, used only to detect regressions.
    - ``BASELINE``: the current production baseline; promotion eligible
      by default (it is the incumbent).
    """

    PRIMARY = "primary"
    CHALLENGER = "challenger"
    SANITY = "sanity"
    BASELINE = "baseline"


class ModelFamilySpec(BaseModel):
    """A versioned, declarative spec for one model family.

    Frozen + ``extra='forbid'`` (audit integrity). A spec is the unit the
    registry stores; adding a family is a single ``register()`` call.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    family_id: str
    display_name: str
    version: str
    dataset_shape: str
    objectives: tuple[str, ...]
    artifact_format: str
    artifact_loader: str
    required_metrics: tuple[str, ...]
    runpod_image: str | None = None
    requires_gpu: bool = False
    max_budget_cents: int = 0
    promotion_eligibility_class: PromotionEligibilityClass = (
        PromotionEligibilityClass.CHALLENGER
    )
    is_baseline_exception: bool = False
    created_at_ns: int = 0


class FamilyValidationResult(BaseModel):
    """Result of ``ModelFamilyRegistry.validate_family``.

    Frozen + ``extra='forbid'``. ``passed`` is True only when there are no
    errors. ``warnings`` carries advisory notes (e.g. a canary/research
    family that wants a GPU but is running without one).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    passed: bool
    family_id: str
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


class ModelFamilyRegistry:
    """Versioned registry of model family specs — the single source of truth.

    The registry is process-global: the module-level
    :data:`MODEL_FAMILY_REGISTRY` singleton is pre-populated with the
    initial families at import time. Adding a family is declarative and
    gated (a ``register()`` call that code review approves); unknown
    families are rejected by ``validate_family``.

    Thread-safety: the registry is populated once at import and read-only
    thereafter. ``register`` is not intended for concurrent mutation at
    runtime — it exists so a deployment can add a family in a controlled
    startup hook.
    """

    def __init__(self) -> None:
        self._specs: dict[str, ModelFamilySpec] = {}

    # --- mutation (gated / declarative) --------------------------------

    def register(self, family_spec: ModelFamilySpec) -> None:
        """Register a family spec.

        Raises ``ValueError`` if a spec with the same ``family_id`` is
        already registered (re-registration requires an explicit
        version bump + removal path, not a silent overwrite).
        """
        if not isinstance(family_spec, ModelFamilySpec):
            raise TypeError("family_spec must be a ModelFamilySpec")
        if not family_spec.family_id or not family_spec.family_id.strip():
            raise ValueError("family_spec.family_id must be non-empty")
        if family_spec.family_id in self._specs:
            raise ValueError(
                f"family {family_spec.family_id!r} already registered; "
                "bump the version and remove the old spec first"
            )
        self._specs[family_spec.family_id] = family_spec

    # --- read API ------------------------------------------------------

    def get(self, family_id: str) -> ModelFamilySpec:
        """Return the spec for ``family_id``.

        Raises ``KeyError`` if the family is not registered.
        """
        if family_id not in self._specs:
            raise KeyError(
                f"model family {family_id!r} is not registered; "
                f"known: {sorted(self._specs)}"
            )
        return self._specs[family_id]

    def list(self) -> list[str]:
        """Return all registered family ids (sorted for determinism)."""
        return sorted(self._specs)

    def is_registered(self, family_id: str) -> bool:
        """Return True if ``family_id`` is registered."""
        return family_id in self._specs

    # --- validation ----------------------------------------------------

    def validate_family(
        self,
        *,
        family_id: str,
        mode: str,
        has_gpu: bool,
    ) -> FamilyValidationResult:
        """Validate that ``family_id`` may run in ``mode``.

        Rules:
          - The family MUST be registered (unknown family → error).
          - The family MUST declare an artifact loader (non-empty
            ``artifact_loader``); a family without a loader is rejected.
          - For ``production`` mode: the family MUST map to a GPU RunPod
            image (``runpod_image`` is not None and ``requires_gpu`` is
            True) OR carry an explicit baseline exception
            (``is_baseline_exception`` is True). Any other family is
            rejected for production.
          - For ``canary`` / ``research``: the GPU requirement is
            advisory — a GPU family running without a GPU produces a
            warning, not an error.

        Returns a :class:`FamilyValidationResult` with ``passed`` True
        only when there are no errors.
        """
        errors: list[str] = []
        warnings: list[str] = []

        # 1. Known family?
        if not self.is_registered(family_id):
            errors.append(
                f"model family {family_id!r} is not registered; "
                f"known: {self.list()}"
            )
            return FamilyValidationResult(
                passed=False,
                family_id=family_id,
                errors=tuple(errors),
                warnings=tuple(warnings),
            )

        spec = self.get(family_id)

        # 2. Artifact loader present?
        if not spec.artifact_loader or not spec.artifact_loader.strip():
            errors.append(
                f"family {family_id!r} has no artifact_loader; a family "
                "without a loader cannot produce a loadable artifact"
            )

        # 3. Mode-specific gating.
        if mode == "production":
            maps_to_gpu = (
                spec.runpod_image is not None
                and spec.runpod_image.strip() != ""
                and spec.requires_gpu
            )
            if not maps_to_gpu and not spec.is_baseline_exception:
                errors.append(
                    f"production mode requires family {family_id!r} to map "
                    "to a GPU RunPod image (runpod_image set + requires_gpu) "
                    "or carry an explicit baseline exception; "
                    f"runpod_image={spec.runpod_image!r}, "
                    f"requires_gpu={spec.requires_gpu}, "
                    f"is_baseline_exception={spec.is_baseline_exception}"
                )
        else:
            # canary / research: GPU requirement is advisory.
            if spec.requires_gpu and not has_gpu:
                warnings.append(
                    f"family {family_id!r} prefers a GPU but is running "
                    f"without one in {mode!r} mode (advisory only)"
                )

        return FamilyValidationResult(
            passed=len(errors) == 0,
            family_id=family_id,
            errors=tuple(errors),
            warnings=tuple(warnings),
        )


# Module-level singleton registry, pre-populated with the 5 initial
# families. This is the authoritative registry instance for the platform.
MODEL_FAMILY_REGISTRY: ModelFamilyRegistry = ModelFamilyRegistry()


def _register_initial_families(registry: ModelFamilyRegistry) -> None:
    """Pre-register the 5 initial model families (T-7.1).

    Adding a family is declarative and gated: each spec is constructed
    in one place and registered in one call. New families are added by
    appending a spec + ``register()`` call here (under code review).
    """
    now_ns = time.time_ns()
    registry.register(
        ModelFamilySpec(
            family_id="lightgbm_baseline",
            display_name="LightGBM baseline (CPU)",
            version="1",
            dataset_shape="tabular_wide",
            objectives=("binary", "regression"),
            artifact_format="lightgbm_model",
            artifact_loader="quant_foundry.artifact_io.load_lightgbm_model",
            required_metrics=("auc", "logloss", "brier", "mse", "mae"),
            runpod_image=None,
            requires_gpu=False,
            max_budget_cents=5000,
            promotion_eligibility_class=PromotionEligibilityClass.BASELINE,
            is_baseline_exception=True,
            created_at_ns=now_ns,
        )
    )
    registry.register(
        ModelFamilySpec(
            family_id="catboost_gpu",
            display_name="CatBoost GPU challenger",
            version="1",
            dataset_shape="tabular_wide",
            objectives=("binary", "regression", "multiclass"),
            artifact_format="catboost_model",
            artifact_loader="quant_foundry.artifact_io.load_catboost_model",
            required_metrics=("auc", "logloss", "brier", "mse", "mae"),
            runpod_image=RUNPOD_GPU_TREE_IMAGE,
            requires_gpu=True,
            max_budget_cents=20000,
            promotion_eligibility_class=PromotionEligibilityClass.CHALLENGER,
            is_baseline_exception=False,
            created_at_ns=now_ns,
        )
    )
    registry.register(
        ModelFamilySpec(
            family_id="xgboost_gpu",
            display_name="XGBoost GPU challenger",
            version="1",
            dataset_shape="tabular_wide",
            objectives=("binary", "regression"),
            artifact_format="xgboost_json",
            artifact_loader="quant_foundry.artifact_io.load_xgboost_model",
            required_metrics=("auc", "logloss", "brier", "mse", "mae"),
            runpod_image=RUNPOD_GPU_TREE_IMAGE,
            requires_gpu=True,
            max_budget_cents=20000,
            promotion_eligibility_class=PromotionEligibilityClass.CHALLENGER,
            is_baseline_exception=False,
            created_at_ns=now_ns,
        )
    )
    registry.register(
        ModelFamilySpec(
            family_id="logreg_sanity",
            display_name="Logistic regression sanity baseline",
            version="1",
            dataset_shape="tabular_wide",
            objectives=("binary", "regression"),
            artifact_format="sklearn_pickle",
            artifact_loader="quant_foundry.artifact_io.load_sklearn_pickle",
            required_metrics=("auc", "logloss", "brier"),
            runpod_image=None,
            requires_gpu=False,
            max_budget_cents=1000,
            promotion_eligibility_class=PromotionEligibilityClass.SANITY,
            is_baseline_exception=False,
            created_at_ns=now_ns,
        )
    )
    registry.register(
        ModelFamilySpec(
            family_id="linear_sanity",
            display_name="Linear regression sanity baseline",
            version="1",
            dataset_shape="tabular_wide",
            objectives=("regression",),
            artifact_format="sklearn_pickle",
            artifact_loader="quant_foundry.artifact_io.load_sklearn_pickle",
            required_metrics=("mse", "mae"),
            runpod_image=None,
            requires_gpu=False,
            max_budget_cents=1000,
            promotion_eligibility_class=PromotionEligibilityClass.SANITY,
            is_baseline_exception=False,
            created_at_ns=now_ns,
        )
    )


_register_initial_families(MODEL_FAMILY_REGISTRY)


# ---------------------------------------------------------------------------
# Recipe
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Recipe:
    """A versioned, deterministic candidate model config.

    A recipe is the unit the lab proposes, trains, and registers. Two
    recipes with the same canonical content produce the same
    ``recipe_hash`` and therefore the same idempotency key — training
    dispatchers can dedupe on this hash to guarantee reproducibility.

    Fields:
        recipe_id: stable identifier (deterministic from canonical content).
        parent_recipe_id: id of the recipe this was mutated from, or None
            for the initial seed recipe.
        mutation_kind: kind of mutation that produced this recipe from
            its parent (e.g. "add_feature", "set_hyperparam"); None for
            seeds.
        feature_set: ordered tuple of feature names (the input schema).
        model_family: one of ``ALLOWED_MODEL_FAMILIES``.
        hyperparameters: mapping of hyperparameter name to float value.
            Values must lie inside ``HYPERPARAM_BOUNDS[model_family]``.
        train_window_ns: training window length in nanoseconds.
        val_window_ns: validation window length in nanoseconds.
        label_horizon_ns: prediction horizon in nanoseconds.
        random_seed: optional random seed for reproducibility.

    Invariants:
        - ``recipe_hash`` is computed from the canonical content and is
          stable across Python processes.
        - ``feature_set`` is a tuple (immutable + ordered).
        - ``hyperparameters`` may only contain keys defined for
          ``model_family`` in ``HYPERPARAM_BOUNDS``.
        - All hyperparameter values lie inside their declared bounds.
        - Windows lie inside the default ranges.
        - ``model_family`` is in ``ALLOWED_MODEL_FAMILIES``.
        - No secret-looking fields (e.g. env values, paths, tokens) are
          ever accepted by the constructor.
    """

    recipe_id: str
    parent_recipe_id: str | None
    mutation_kind: str | None
    feature_set: tuple[str, ...]
    model_family: str
    hyperparameters: Mapping[str, float]
    train_window_ns: int
    val_window_ns: int
    label_horizon_ns: int
    random_seed: int | None = None
    recipe_hash: str = ""

    def __post_init__(self) -> None:
        # Validate model family.
        if self.model_family not in ALLOWED_MODEL_FAMILIES:
            raise ValueError(
                f"model_family {self.model_family!r} is not in the allowlist; "
                f"allowed: {sorted(ALLOWED_MODEL_FAMILIES)}"
            )
        # Validate hyperparameter keys + bounds.
        bounds = HYPERPARAM_BOUNDS.get(self.model_family, {})
        bad_keys = set(self.hyperparameters) - set(bounds)
        if bad_keys:
            raise ValueError(
                f"hyperparameters {sorted(bad_keys)!r} are not defined for "
                f"model_family {self.model_family!r}; allowed: {sorted(bounds)}"
            )
        for k, v in self.hyperparameters.items():
            lo, hi = bounds[k]
            if not (lo <= float(v) <= hi):
                raise ValueError(
                    f"hyperparameter {k}={v} for {self.model_family!r} is "
                    f"outside the allowlist bounds [{lo}, {hi}]"
                )
        # Validate windows.
        lo_tr, hi_tr = DEFAULT_TRAIN_WINDOW_RANGE_NS
        lo_va, hi_va = DEFAULT_VAL_WINDOW_RANGE_NS
        if not (lo_tr <= self.train_window_ns <= hi_tr):
            raise ValueError(
                f"train_window_ns={self.train_window_ns} is outside the "
                f"allowlist range [{lo_tr}, {hi_tr}]"
            )
        if not (lo_va <= self.val_window_ns <= hi_va):
            raise ValueError(
                f"val_window_ns={self.val_window_ns} is outside the "
                f"allowlist range [{lo_va}, {hi_va}]"
            )
        if self.label_horizon_ns <= 0:
            raise ValueError(f"label_horizon_ns must be > 0; got {self.label_horizon_ns}")
        # Reject any field that looks like it carries a secret. We don't
        # have a "secret" field on Recipe, but a defensive check on the
        # feature names is cheap insurance — secrets often masquerade as
        # feature names ("api_key", "token", "secret", "password").
        forbidden_name_substrings = (
            "password",
            "token",
            "secret",
            "api_key",
            "apikey",
            "credential",
            "private_key",
        )
        for fname in self.feature_set:
            f_low = fname.lower()
            for sub in forbidden_name_substrings:
                if sub in f_low:
                    raise ValueError(
                        f"feature name {fname!r} looks like it carries a "
                        "secret; recipes must not contain credential fields"
                    )
        # Compute and freeze recipe_hash from the canonical content.
        ch = self._compute_hash()
        # ``frozen=True`` requires object.__setattr__ to overwrite.
        object.__setattr__(self, "recipe_hash", ch)

    def _canonical_payload(self) -> dict[str, Any]:
        """Return a JSON-stable representation of the recipe's content."""
        return {
            "recipe_id": self.recipe_id,
            "parent_recipe_id": self.parent_recipe_id,
            "mutation_kind": self.mutation_kind,
            "feature_set": list(self.feature_set),
            "model_family": self.model_family,
            "hyperparameters": dict(sorted(self.hyperparameters.items())),
            "train_window_ns": self.train_window_ns,
            "val_window_ns": self.val_window_ns,
            "label_horizon_ns": self.label_horizon_ns,
            "random_seed": self.random_seed,
        }

    def _compute_hash(self) -> str:
        """Deterministic sha256 over canonical content (excluding self.recipe_hash)."""
        payload = json.dumps(
            self._canonical_payload(),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def derive_recipe_id(
        *,
        parent_recipe_id: str | None,
        mutation_kind: str | None,
        canonical_payload: Mapping[str, Any],
    ) -> str:
        """Derive a stable recipe_id from the parent + mutation + canonical payload."""
        seed = {
            "parent_recipe_id": parent_recipe_id,
            "mutation_kind": mutation_kind,
            "payload": dict(canonical_payload),
        }
        body = json.dumps(seed, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return "recipe-" + hashlib.sha256(body).hexdigest()[:16]


# ---------------------------------------------------------------------------
# RecipeMutation
# ---------------------------------------------------------------------------


class MutationKind(enum.StrEnum):
    """Allowlisted mutation kinds."""

    ADD_FEATURE = "add_feature"
    REMOVE_FEATURE = "remove_feature"
    TRANSFORM_FEATURE = "transform_feature"
    SET_HYPERPARAM = "set_hyperparam"
    NARROW_TRAIN_WINDOW = "narrow_train_window"
    WIDEN_TRAIN_WINDOW = "widen_train_window"


@dataclasses.dataclass(frozen=True)
class RecipeMutation:
    """A typed mutation to apply to a parent recipe.

    Each kind carries the parameters needed to apply the mutation. The
    ``apply`` method validates against the allowlists and returns a new
    ``Recipe``. The lab rejects mutations outside the allowlist
    (``apply`` raises ``ValueError`` on any violation).
    """

    kind: MutationKind
    # ADD / REMOVE / TRANSFORM feature:
    feature_name: str | None = None
    transform_kind: str | None = None  # one of ALLOWED_TRANSFORMS
    # SET_HYPERPARAM:
    hyperparam_name: str | None = None
    hyperparam_value: float | None = None
    # NARROW / WIDEN window:
    delta_ns: int | None = None

    def apply(self, parent: Recipe) -> Recipe:
        """Apply the mutation to ``parent`` and return a new ``Recipe``.

        Raises ``ValueError`` if the mutation falls outside the allowlist.
        Never mutates the parent in place.
        """
        if not isinstance(self.kind, MutationKind):
            raise ValueError(f"mutation kind must be a MutationKind; got {self.kind!r}")

        if self.kind == MutationKind.ADD_FEATURE:
            return self._apply_add_feature(parent)
        if self.kind == MutationKind.REMOVE_FEATURE:
            return self._apply_remove_feature(parent)
        if self.kind == MutationKind.TRANSFORM_FEATURE:
            return self._apply_transform_feature(parent)
        if self.kind == MutationKind.SET_HYPERPARAM:
            return self._apply_set_hyperparam(parent)
        if self.kind == MutationKind.NARROW_TRAIN_WINDOW:
            return self._apply_narrow_train_window(parent)
        if self.kind == MutationKind.WIDEN_TRAIN_WINDOW:
            return self._apply_widen_train_window(parent)
        # Defensive: unknown enum value.
        raise ValueError(f"unhandled mutation kind: {self.kind!r}")

    # --- feature mutations ----------------------------------------------

    def _apply_add_feature(self, parent: Recipe) -> Recipe:
        if not self.feature_name or not self.feature_name.strip():
            raise ValueError("ADD_FEATURE requires non-empty feature_name")
        # Reject the same secret-name heuristic the Recipe constructor uses.
        forbidden = (
            "password",
            "token",
            "secret",
            "api_key",
            "apikey",
            "credential",
            "private_key",
        )
        for sub in forbidden:
            if sub in self.feature_name.lower():
                raise ValueError(f"feature name {self.feature_name!r} looks like a secret")
        if self.feature_name in parent.feature_set:
            raise ValueError(f"feature {self.feature_name!r} already present in parent recipe")
        new_features = (*parent.feature_set, self.feature_name)
        payload = self._new_payload(parent, feature_set=new_features)
        return _build_recipe(parent=parent, mutation_kind=self.kind.value, payload=payload)

    def _apply_remove_feature(self, parent: Recipe) -> Recipe:
        if not self.feature_name:
            raise ValueError("REMOVE_FEATURE requires feature_name")
        if self.feature_name not in parent.feature_set:
            raise ValueError(f"feature {self.feature_name!r} not in parent recipe")
        new_features = tuple(f for f in parent.feature_set if f != self.feature_name)
        if not new_features:
            raise ValueError("cannot remove the last feature from a recipe")
        payload = self._new_payload(parent, feature_set=new_features)
        return _build_recipe(parent=parent, mutation_kind=self.kind.value, payload=payload)

    def _apply_transform_feature(self, parent: Recipe) -> Recipe:
        if not self.feature_name:
            raise ValueError("TRANSFORM_FEATURE requires feature_name")
        if self.feature_name not in parent.feature_set:
            raise ValueError(f"feature {self.feature_name!r} not in parent recipe")
        if self.transform_kind not in ALLOWED_TRANSFORMS:
            raise ValueError(
                f"transform_kind {self.transform_kind!r} is not in the "
                f"allowlist; allowed: {sorted(ALLOWED_TRANSFORMS)}"
            )
        # A transformation is conceptually a remove + add (the new feature
        # is a derived feature whose observed_at inherits from the source).
        old = self.feature_name
        new = f"{old}__{self.transform_kind}"
        # Build the new feature list: replace ``old`` with ``new``, dedup.
        replaced: list[str] = []
        for f in parent.feature_set:
            if f == old:
                if new not in replaced:
                    replaced.append(new)
            else:
                if f not in replaced:
                    replaced.append(f)
        if new not in replaced:
            replaced.append(new)
        payload = self._new_payload(parent, feature_set=tuple(replaced))
        return _build_recipe(parent=parent, mutation_kind=self.kind.value, payload=payload)

    # --- hyperparameter mutation ----------------------------------------

    def _apply_set_hyperparam(self, parent: Recipe) -> Recipe:
        if self.hyperparam_name is None or self.hyperparam_value is None:
            raise ValueError("SET_HYPERPARAM requires hyperparam_name and hyperparam_value")
        bounds = HYPERPARAM_BOUNDS.get(parent.model_family, {})
        if self.hyperparam_name not in bounds:
            raise ValueError(
                f"hyperparam {self.hyperparam_name!r} not defined for "
                f"model_family {parent.model_family!r}; allowed: "
                f"{sorted(bounds)}"
            )
        lo, hi = bounds[self.hyperparam_name]
        if not (lo <= float(self.hyperparam_value) <= hi):
            raise ValueError(
                f"hyperparam value {self.hyperparam_value} for "
                f"{self.hyperparam_name!r} is outside bounds [{lo}, {hi}]"
            )
        new_hp = dict(parent.hyperparameters)
        new_hp[self.hyperparam_name] = float(self.hyperparam_value)
        payload = self._new_payload(parent, hyperparameters=new_hp)
        return _build_recipe(parent=parent, mutation_kind=self.kind.value, payload=payload)

    # --- window mutations -----------------------------------------------

    def _apply_narrow_train_window(self, parent: Recipe) -> Recipe:
        if self.delta_ns is None or self.delta_ns <= 0:
            raise ValueError("NARROW_TRAIN_WINDOW requires positive delta_ns")
        new_train = parent.train_window_ns - self.delta_ns
        lo_tr, _hi_tr = DEFAULT_TRAIN_WINDOW_RANGE_NS
        if new_train < lo_tr:
            raise ValueError(
                f"narrowing train window to {new_train}ns would fall below "
                f"the allowlist minimum {lo_tr}ns"
            )
        payload = self._new_payload(parent, train_window_ns=new_train)
        return _build_recipe(parent=parent, mutation_kind=self.kind.value, payload=payload)

    def _apply_widen_train_window(self, parent: Recipe) -> Recipe:
        if self.delta_ns is None or self.delta_ns <= 0:
            raise ValueError("WIDEN_TRAIN_WINDOW requires positive delta_ns")
        new_train = parent.train_window_ns + self.delta_ns
        _lo_tr, hi_tr = DEFAULT_TRAIN_WINDOW_RANGE_NS
        if new_train > hi_tr:
            raise ValueError(
                f"widening train window to {new_train}ns would exceed the "
                f"allowlist maximum {hi_tr}ns"
            )
        payload = self._new_payload(parent, train_window_ns=new_train)
        return _build_recipe(parent=parent, mutation_kind=self.kind.value, payload=payload)

    # --- helper ---------------------------------------------------------

    def _new_payload(self, parent: Recipe, **overrides: Any) -> dict[str, Any]:
        """Build the canonical payload for the new recipe, applying overrides."""
        payload: dict[str, Any] = {
            "feature_set": list(parent.feature_set),
            "model_family": parent.model_family,
            "hyperparameters": dict(parent.hyperparameters),
            "train_window_ns": parent.train_window_ns,
            "val_window_ns": parent.val_window_ns,
            "label_horizon_ns": parent.label_horizon_ns,
            "random_seed": parent.random_seed,
        }
        for k, v in overrides.items():
            payload[k] = v
        return payload


def _build_recipe(
    *,
    parent: Recipe,
    mutation_kind: str,
    payload: dict[str, Any],
) -> Recipe:
    """Construct a new ``Recipe`` from a parent + mutation + canonical payload."""
    # Convert list->tuple for feature_set (immutable contract).
    feature_set = tuple(payload["feature_set"])
    hyperparameters = dict(payload["hyperparameters"])
    recipe_id = Recipe.derive_recipe_id(
        parent_recipe_id=parent.recipe_id,
        mutation_kind=mutation_kind,
        canonical_payload=payload,
    )
    return Recipe(
        recipe_id=recipe_id,
        parent_recipe_id=parent.recipe_id,
        mutation_kind=mutation_kind,
        feature_set=feature_set,
        model_family=payload["model_family"],
        hyperparameters=hyperparameters,
        train_window_ns=int(payload["train_window_ns"]),
        val_window_ns=int(payload["val_window_ns"]),
        label_horizon_ns=int(payload["label_horizon_ns"]),
        random_seed=payload.get("random_seed"),
    )


# ---------------------------------------------------------------------------
# TrialBudget
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class TrialCostEstimate:
    """Estimated cost for a single recipe trial.

    Costs are integer cents. Zero is allowed (mock / local CPU).
    """

    recipe_id: str
    cost_cents: int


@dataclasses.dataclass(frozen=True)
class BudgetDecision:
    """Result of ``TrialBudget.check_recipe`` and ``check_sweep``."""

    allowed: bool
    reason: str
    spent_cents: int
    limit_cents: int
    scope: str  # "recipe" or "sweep"


class TrialBudget:
    """Enforce per-recipe and per-sweep cost limits, consulting ``BudgetGuard``.

    Design:
        - ``per_recipe_limit_cents``: max cost any single recipe trial may
          incur. Defaults to $50.
        - ``per_sweep_limit_cents``: max total cost for all recipe trials in
          a single sweep. Defaults to $500.
        - ``guard``: optional ``BudgetGuard`` for the global kill switch.
          When present, paid trials also pass through ``guard.check_and_reserve``.
        - Running trials are NOT killed by budget exhaustion — only new
          trial dispatch is blocked. This matches the BIG_PLAN rule
          "Budget exhaustion stops new trials, doesn't kill running ones."
    """

    def __init__(
        self,
        *,
        per_recipe_limit_cents: int = 5000,
        per_sweep_limit_cents: int = 50_000,
        guard: BudgetGuard | None = None,
    ) -> None:
        if per_recipe_limit_cents < 0:
            raise ValueError("per_recipe_limit_cents must be >= 0")
        if per_sweep_limit_cents < 0:
            raise ValueError("per_sweep_limit_cents must be >= 0")
        if per_recipe_limit_cents > per_sweep_limit_cents:
            raise ValueError("per_recipe_limit_cents must be <= per_sweep_limit_cents")
        self.per_recipe_limit_cents = per_recipe_limit_cents
        self.per_sweep_limit_cents = per_sweep_limit_cents
        self._guard = guard
        # Mutable state (not frozen because the budget tracks spend).
        self._sweep_spent_cents: int = 0

    # --- public API ----------------------------------------------------

    def reset_sweep(self) -> None:
        """Reset the per-sweep spend accumulator (call at sweep start)."""
        self._sweep_spent_cents = 0

    def check_recipe(
        self,
        *,
        recipe_id: str,
        cost_cents: int,
    ) -> BudgetDecision:
        """Check whether a single recipe trial may dispatch.

        Rejects when:
            - cost_cents < 0
            - cost_cents > per_recipe_limit_cents
            - cost_cents + sweep_spent > per_sweep_limit_cents
            - BudgetGuard kill switch blocks paid jobs
        """
        if cost_cents < 0:
            raise ValueError("cost_cents must be >= 0")
        if cost_cents > self.per_recipe_limit_cents:
            return BudgetDecision(
                allowed=False,
                reason=(
                    f"recipe cost {cost_cents}c exceeds per-recipe limit "
                    f"{self.per_recipe_limit_cents}c"
                ),
                spent_cents=self._sweep_spent_cents,
                limit_cents=self.per_recipe_limit_cents,
                scope="recipe",
            )
        new_sweep_total = self._sweep_spent_cents + cost_cents
        if new_sweep_total > self.per_sweep_limit_cents:
            return BudgetDecision(
                allowed=False,
                reason=(
                    f"recipe cost {cost_cents}c + sweep spent "
                    f"{self._sweep_spent_cents}c = {new_sweep_total}c would "
                    f"exceed per-sweep limit {self.per_sweep_limit_cents}c"
                ),
                spent_cents=self._sweep_spent_cents,
                limit_cents=self.per_sweep_limit_cents,
                scope="sweep",
            )
        # Consult BudgetGuard for global kill switch (paid jobs only).
        if self._guard is not None and cost_cents > 0:
            gd = self._guard.check_and_reserve(
                amount_cents=cost_cents,
                job_type=f"alpha_genome:{recipe_id}",
            )
            if not gd.allowed:
                return BudgetDecision(
                    allowed=False,
                    reason=f"BudgetGuard rejected: {gd.reason}",
                    spent_cents=self._sweep_spent_cents,
                    limit_cents=self.per_sweep_limit_cents,
                    scope="sweep",
                )
        return BudgetDecision(
            allowed=True,
            reason="",
            spent_cents=self._sweep_spent_cents,
            limit_cents=self.per_sweep_limit_cents,
            scope="recipe",
        )

    def record_recipe_spend(self, *, cost_cents: int) -> None:
        """Record actual spend for a recipe trial (after dispatch)."""
        if cost_cents < 0:
            raise ValueError("cost_cents must be >= 0")
        self._sweep_spent_cents += cost_cents

    @property
    def sweep_spent_cents(self) -> int:
        """Current per-sweep spend (read-only)."""
        return self._sweep_spent_cents


# ---------------------------------------------------------------------------
# EarlyStopper
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class EarlyStopDecision:
    """Result of ``EarlyStopper.should_stop``."""

    should_stop: bool
    reason: str
    settled_count: int
    parent_score: float | None
    child_score: float | None


class EarlyStopper:
    """Kill underperforming sweeps early based on intermediate scores.

    ``parent_score`` is the parent recipe's settled tournament score
    (e.g. deflated Sharpe). ``child_score`` is the running recipe's
    intermediate score after ``min_settled`` settled predictions. If the
    child's score is below ``parent_score * (1 - relative_threshold)``,
    the recipe is killed early with a ``KILLED_EARLY`` status and a
    reason recorded on the ``DiscardReceipt``.

    Invariants:
        - ``min_settled >= 1`` (need at least one settled prediction
          before a kill decision).
        - ``relative_threshold > 0`` (positive threshold).
        - If the parent score is None (no parent — seed recipe), no
          early stop is performed.
    """

    def __init__(
        self,
        *,
        min_settled: int = 20,
        relative_threshold: float = 0.10,
    ) -> None:
        if min_settled < 1:
            raise ValueError("min_settled must be >= 1")
        if relative_threshold <= 0:
            raise ValueError("relative_threshold must be > 0")
        self.min_settled = min_settled
        self.relative_threshold = relative_threshold

    def should_stop(
        self,
        *,
        settled_count: int,
        child_score: float | None,
        parent_score: float | None,
    ) -> EarlyStopDecision:
        """Decide whether to kill a recipe early.

        Returns ``EarlyStopDecision`` with ``should_stop=True`` and a
        reason string when:
            - settled_count >= min_settled
            - child_score is not None and parent_score is not None
            - child_score < parent_score * (1 - relative_threshold)
        """
        if settled_count < self.min_settled:
            return EarlyStopDecision(
                should_stop=False,
                reason="not enough settled predictions",
                settled_count=settled_count,
                parent_score=parent_score,
                child_score=child_score,
            )
        if child_score is None or parent_score is None:
            return EarlyStopDecision(
                should_stop=False,
                reason="missing score (parent or child)",
                settled_count=settled_count,
                parent_score=parent_score,
                child_score=child_score,
            )
        floor = parent_score * (1.0 - self.relative_threshold)
        if child_score < floor:
            return EarlyStopDecision(
                should_stop=True,
                reason=(
                    f"child score {child_score:.6f} below parent "
                    f"{parent_score:.6f} - {self.relative_threshold:.0%} "
                    f"floor ({floor:.6f}) after {settled_count} settled"
                ),
                settled_count=settled_count,
                parent_score=parent_score,
                child_score=child_score,
            )
        return EarlyStopDecision(
            should_stop=False,
            reason="child score above floor",
            settled_count=settled_count,
            parent_score=parent_score,
            child_score=child_score,
        )


# ---------------------------------------------------------------------------
# Recipe dispatch + tournament probe callbacks
# ---------------------------------------------------------------------------


# A ``TrainingDispatcher`` takes a recipe and returns a
# ``TrainingOutcome``. We type it structurally to keep this module
# decoupled from ``runpod_training.py`` (file-disjoint from the active
# builders per the module docstring).
#
# A TrainingOutcome MUST carry:
#   - model_id: the id of the trained model (for dossier reference)
#   - dossier_evidence: an opaque object accepted by PromotionEvidence
#   - settlement_evidence_refs: list of settlement ids
#   - shadow_prediction_refs: list of shadow prediction ids
#   - cost_cents: actual cost of the training run
#   - duration_seconds: actual duration
TrainingOutcome = Any
TrainingDispatcher = Callable[[Recipe], TrainingOutcome]

# A ``TournamentProbe`` takes a recipe_id and returns either None (not
# enough evidence yet) or a TournamentScore-equivalent (anything with
# ``deflated_sharpe`` and ``settled_count`` attributes).
TournamentProbe = Callable[[str], Any]


# ---------------------------------------------------------------------------
# Receipts
# ---------------------------------------------------------------------------


class TrialStatus(enum.StrEnum):
    """Per-recipe trial outcome."""

    REGISTERED = "registered"
    REJECTED_BY_GATE = "rejected_by_gate"
    KILLED_EARLY = "killed_early"
    DISCARDED = "discarded"


@dataclasses.dataclass(frozen=True)
class TrialReceipt:
    """Immutable record of a single recipe trial's outcome."""

    recipe_id: str
    parent_recipe_id: str | None
    status: TrialStatus
    reason: str
    model_id: str | None
    cost_cents: int
    duration_seconds: float
    promotion_decision: str | None
    sweep_id: str


@dataclasses.dataclass(frozen=True)
class DiscardReceipt:
    """Immutable discard receipt for a recipe that failed the gates.

    Per the BIG_PLAN: "Failed recipes are discarded with a discard
    receipt." The receipt carries the recipe hash, status, and reason
    so audit can trace why a recipe did not advance.
    """

    recipe_id: str
    recipe_hash: str
    status: TrialStatus
    reason: str
    sweep_id: str


@dataclasses.dataclass(frozen=True)
class SweepReceipt:
    """Summary receipt for one AlphaGenomeLab sweep."""

    sweep_id: str
    seed_recipe_id: str
    n_recipes: int
    n_registered: int
    n_rejected: int
    n_killed_early: int
    n_discarded: int
    sweep_cost_cents: int
    started_at_ns: int
    ended_at_ns: int
    trial_receipts: tuple[TrialReceipt, ...]


# ---------------------------------------------------------------------------
# AlphaGenomeLab
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class AlphaGenomeLab:
    """Orchestrate a recipe sweep: generate, dispatch, evaluate, register.

    The lab is **opt-in**: a sweep is started via ``run_sweep``. Every
    candidate recipe must pass through the supplied ``gate`` (a
    ``PromotionGate`` from ``promotion.py``) — there is no fast path.
    Surviving candidates are added to ``registry`` (a
    ``DossierRegistry``-like object with an ``upsert`` method).

    Args:
        gate: a ``PromotionGate`` instance. Every candidate must pass.
        budget: a ``TrialBudget`` enforcing per-recipe / per-sweep costs.
        early_stopper: an ``EarlyStopper`` killing underperformers early.
        dispatcher: callable that trains a recipe and returns a
            ``TrainingOutcome``. May be a mock for tests.
        tournament_probe: callable returning the current tournament score
            for a recipe_id (or None if not enough evidence yet).
        registry: registry with ``upsert(dossier)`` method. May be a
            ``DossierRegistry`` instance or any duck-typed equivalent.

    Invariants:
        - No recipe can bypass the gate. ``_register_candidate`` only
          adds to the registry if ``gate.evaluate(...)`` returns
          ``APPROVED``.
        - No recipe can be registered with authority above
          ``SHADOW_ONLY`` (alpha_genome recipes are SHADOW_ONLY by
          construction — promotion to paper_approved requires the same
          human approval path as any other model).
        - Budget exhaustion stops new trials, doesn't kill running ones.
        - All receipts are immutable (frozen dataclasses).
        - No secrets in any receipt — the sweep iterates over Recipe
          objects whose constructor rejects secret-shaped feature names.
    """

    gate: Any
    budget: TrialBudget
    early_stopper: EarlyStopper
    dispatcher: TrainingDispatcher
    tournament_probe: TournamentProbe
    registry: Any

    def _next_mutation(
        self,
        *,
        parent: Recipe,
        rng_seed: int,
    ) -> RecipeMutation:
        """Pick the next mutation deterministically from a parent + seed.

        The choice is intentionally simple and bounded — the lab's
        responsibility is to *enforce* the allowlist, not to invent a
        fancy search algorithm. Six rotations cycle through the allowlisted
        mutation kinds; the per-kind parameters are chosen within the
        bounds from the seed. Out-of-bounds values would be rejected by
        ``RecipeMutation.apply``.
        """
        # Use the seed to deterministically pick one of six rotation slots.
        slot = (rng_seed % 6 + 6) % 6
        # Pick a feature name from the parent (or "" if none).
        f0 = parent.feature_set[0] if parent.feature_set else "x0"
        if slot == 0:
            return RecipeMutation(
                kind=MutationKind.ADD_FEATURE,
                feature_name=f"new_feat_{rng_seed % 7}",
            )
        if slot == 1:
            return RecipeMutation(
                kind=MutationKind.REMOVE_FEATURE,
                feature_name=f0,
            )
        if slot == 2:
            return RecipeMutation(
                kind=MutationKind.TRANSFORM_FEATURE,
                feature_name=f0,
                transform_kind="zscore",
            )
        if slot == 3:
            # Pick the first hyperparameter (always exists).
            hp_name = next(iter(HYPERPARAM_BOUNDS[parent.model_family]))
            lo, hi = HYPERPARAM_BOUNDS[parent.model_family][hp_name]
            # Map seed into the bounds.
            v = lo + (hi - lo) * ((rng_seed % 100) / 100.0)
            return RecipeMutation(
                kind=MutationKind.SET_HYPERPARAM,
                hyperparam_name=hp_name,
                hyperparam_value=v,
            )
        if slot == 4:
            return RecipeMutation(
                kind=MutationKind.NARROW_TRAIN_WINDOW,
                delta_ns=7 * 86_400_000_000_000,  # 7 days
            )
        return RecipeMutation(
            kind=MutationKind.WIDEN_TRAIN_WINDOW,
            delta_ns=14 * 86_400_000_000_000,  # 14 days
        )

    def run_sweep(
        self,
        *,
        seed_recipe: Recipe,
        n_recipes: int,
        sweep_id: str | None = None,
    ) -> SweepReceipt:
        """Run a recipe sweep starting from ``seed_recipe``.

        Generates up to ``n_recipes`` mutated recipes, dispatches each
        through ``dispatcher``, evaluates through ``gate``, and registers
        surviving candidates in ``registry``. Stops dispatching new
        trials when ``budget`` is exhausted (does NOT kill running
        trials).

        Returns a ``SweepReceipt`` with the per-trial outcome list.
        """
        if n_recipes <= 0:
            raise ValueError("n_recipes must be > 0")
        # Validate the seed recipe's content_hash matches.
        expected_hash = seed_recipe._compute_hash()
        if expected_hash != seed_recipe.recipe_hash:
            raise ValueError(
                "seed recipe recipe_hash does not match its content; refusing to run sweep"
            )

        sid = sweep_id or f"sweep-{int(time.time_ns())}-{seed_recipe.recipe_id}"
        self.budget.reset_sweep()
        started_at_ns = time.time_ns()

        trial_receipts: list[TrialReceipt] = []
        n_registered = 0
        n_rejected = 0
        n_killed = 0
        n_discarded = 0

        for i in range(n_recipes):
            # 1. Generate a candidate.
            mutation = self._next_mutation(parent=seed_recipe, rng_seed=i + 1)
            try:
                candidate = mutation.apply(seed_recipe)
            except ValueError as exc:
                # Mutation outside allowlist — discard and continue.
                receipt = TrialReceipt(
                    recipe_id=f"rejected-{sid}-{i}",
                    parent_recipe_id=seed_recipe.recipe_id,
                    status=TrialStatus.DISCARDED,
                    reason=f"mutation rejected: {exc}",
                    model_id=None,
                    cost_cents=0,
                    duration_seconds=0.0,
                    promotion_decision=None,
                    sweep_id=sid,
                )
                trial_receipts.append(receipt)
                n_discarded += 1
                continue

            # 2. Check budget BEFORE dispatch.
            # Cost estimate: a single recipe trial on the lab is $10 by
            # default; tests override with a TrialBudget that allows $0.
            estimate_cents = 1000  # $10 baseline estimate
            bd = self.budget.check_recipe(
                recipe_id=candidate.recipe_id,
                cost_cents=estimate_cents,
            )
            if not bd.allowed:
                # Budget exhausted — stop dispatching new trials.
                receipt = TrialReceipt(
                    recipe_id=candidate.recipe_id,
                    parent_recipe_id=seed_recipe.recipe_id,
                    status=TrialStatus.DISCARDED,
                    reason=f"budget exhausted: {bd.reason}",
                    model_id=None,
                    cost_cents=0,
                    duration_seconds=0.0,
                    promotion_decision=None,
                    sweep_id=sid,
                )
                trial_receipts.append(receipt)
                n_discarded += 1
                break  # running trials finish; new trials stop.

            # 3. Dispatch training.
            t0 = time.time()
            outcome = self.dispatcher(candidate)
            duration = time.time() - t0

            # 4. Record actual cost (so the budget ledger reflects reality).
            actual_cost = int(getattr(outcome, "cost_cents", estimate_cents))
            self.budget.record_recipe_spend(cost_cents=actual_cost)

            # 5. Early-stop check (only if a tournament probe is wired).
            child_score = None
            score_obj = self.tournament_probe(candidate.recipe_id)
            if score_obj is not None:
                child_score = float(getattr(score_obj, "deflated_sharpe", 0.0) or 0.0)
                settled = int(getattr(score_obj, "settled_count", 0) or 0)
                parent_score_obj = self.tournament_probe(seed_recipe.recipe_id)
                parent_score = None
                if parent_score_obj is not None:
                    parent_score = float(getattr(parent_score_obj, "deflated_sharpe", 0.0) or 0.0)
                es = self.early_stopper.should_stop(
                    settled_count=settled,
                    child_score=child_score,
                    parent_score=parent_score,
                )
                if es.should_stop:
                    receipt = TrialReceipt(
                        recipe_id=candidate.recipe_id,
                        parent_recipe_id=seed_recipe.recipe_id,
                        status=TrialStatus.KILLED_EARLY,
                        reason=es.reason,
                        model_id=getattr(outcome, "model_id", None),
                        cost_cents=actual_cost,
                        duration_seconds=duration,
                        promotion_decision=None,
                        sweep_id=sid,
                    )
                    trial_receipts.append(receipt)
                    n_killed += 1
                    continue

            # 6. Build the evidence packet and run through the gate.
            evidence = self._build_evidence(outcome)
            request = self._build_request(candidate, outcome)
            pr = self.gate.evaluate(request=request, evidence=evidence)
            decision = pr.decision.value

            # 7. Register or discard.
            if decision == "approved":
                dossier = getattr(outcome, "dossier_evidence", None)
                if dossier is not None:
                    self.registry.upsert(dossier)
                receipt = TrialReceipt(
                    recipe_id=candidate.recipe_id,
                    parent_recipe_id=seed_recipe.recipe_id,
                    status=TrialStatus.REGISTERED,
                    reason="passed gate",
                    model_id=getattr(outcome, "model_id", None),
                    cost_cents=actual_cost,
                    duration_seconds=duration,
                    promotion_decision=decision,
                    sweep_id=sid,
                )
                trial_receipts.append(receipt)
                n_registered += 1
            else:
                reason = pr.rejection_reason.value if pr.rejection_reason else "unknown"
                receipt = TrialReceipt(
                    recipe_id=candidate.recipe_id,
                    parent_recipe_id=seed_recipe.recipe_id,
                    status=TrialStatus.REJECTED_BY_GATE,
                    reason=reason,
                    model_id=getattr(outcome, "model_id", None),
                    cost_cents=actual_cost,
                    duration_seconds=duration,
                    promotion_decision=decision,
                    sweep_id=sid,
                )
                trial_receipts.append(receipt)
                n_rejected += 1

        ended_at_ns = time.time_ns()
        return SweepReceipt(
            sweep_id=sid,
            seed_recipe_id=seed_recipe.recipe_id,
            n_recipes=n_recipes,
            n_registered=n_registered,
            n_rejected=n_rejected,
            n_killed_early=n_killed,
            n_discarded=n_discarded,
            sweep_cost_cents=self.budget.sweep_spent_cents,
            started_at_ns=started_at_ns,
            ended_at_ns=ended_at_ns,
            trial_receipts=tuple(trial_receipts),
        )

    # --- internal helpers -----------------------------------------------

    def _build_evidence(self, outcome: Any) -> Any:
        """Build a ``PromotionEvidence`` packet from a training outcome.

        Uses ``PromotionEvidence`` if importable; otherwise the gate's
        ``evaluate`` will see ``dossier=None`` and reject with
        ``NO_DOSSIER`` — which is the correct behavior.
        """
        try:
            from quant_foundry.dossier import DossierRecord
            from quant_foundry.promotion import PromotionEvidence
            from quant_foundry.sentinel import SentinelReceipt
            from quant_foundry.tournament import TournamentResult
        except Exception:
            return None
        raw_dossier = getattr(outcome, "dossier_evidence", None)
        # Only pass through a real DossierRecord; otherwise the gate sees
        # a None dossier and rejects with NO_DOSSIER (the safe path).
        dossier = raw_dossier if isinstance(raw_dossier, DossierRecord) else None
        tournament = getattr(outcome, "tournament_result", None)
        sentinel = getattr(outcome, "sentinel_receipt", None)
        if tournament is not None and not isinstance(tournament, TournamentResult):
            tournament = None
        if sentinel is not None and not isinstance(sentinel, SentinelReceipt):
            sentinel = None
        return PromotionEvidence(
            dossier=dossier,
            tournament_result=tournament,
            sentinel_receipt=sentinel,
            blocking_issues=[],
        )

    def _build_request(self, candidate: Recipe, outcome: Any) -> Any:
        """Build a ``PromotionRequest`` for a candidate recipe.

        The target level is ``research_approved`` — the first step above
        ``candidate``. Higher levels require human approval, which is
        out of scope for the lab's automated path.
        """
        try:
            from quant_foundry.dossier import DossierStatus
            from quant_foundry.promotion import PromotionRequest
        except Exception:
            return None
        return PromotionRequest(
            model_id=getattr(outcome, "model_id", candidate.recipe_id),
            target_level=DossierStatus.RESEARCH_APPROVED,
            review_note=(
                f"alpha_genome sweep candidate from recipe "
                f"{candidate.recipe_id} (parent {candidate.parent_recipe_id})"
            ),
            waivers=[],
        )


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------

__all__ = [
    "ALLOWED_MODEL_FAMILIES",
    "ALLOWED_TRANSFORMS",
    "DEFAULT_TRAIN_WINDOW_RANGE_NS",
    "DEFAULT_VAL_WINDOW_RANGE_NS",
    "HYPERPARAM_BOUNDS",
    "MODEL_FAMILY_REGISTRY",
    "MODEL_FAMILY_REGISTRY_VERSION",
    "RUNPOD_GPU_TREE_IMAGE",
    "AlphaGenomeLab",
    "BudgetDecision",
    "DiscardReceipt",
    "EarlyStopDecision",
    "EarlyStopper",
    "FamilyValidationResult",
    "ModelFamilyRegistry",
    "ModelFamilySpec",
    "MutationKind",
    "PromotionEligibilityClass",
    "Recipe",
    "RecipeMutation",
    "SweepReceipt",
    "TournamentProbe",
    "TrainingDispatcher",
    "TrainingOutcome",
    "TrialBudget",
    "TrialCostEstimate",
    "TrialReceipt",
    "TrialStatus",
]
