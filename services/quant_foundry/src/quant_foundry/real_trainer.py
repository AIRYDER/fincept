"""
quant_foundry.real_trainer — real LightGBM trainer with walk-forward validation.

Replaces the stub ``LocalTrainer`` with actual ML training using LightGBM.
ML dependencies (``lightgbm``, ``numpy``) are imported **lazily** inside
``train()`` so the ``quant_foundry`` package remains importable without ML
deps installed.

Produces a real trained model artifact with:
- Real sha256 hash (from pickled model bytes, not request inputs).
- Real training metrics (accuracy, logloss, Brier score, Sharpe, drawdown,
  win rate) computed from out-of-sample walk-forward predictions.
- Real calibration report (reliability buckets).
- Real feature importance (from LightGBM gain importance).
- Real PBO (probability of backtest overfitting) from fold-level overfit
  detection.
- Real deflated Sharpe ratio.

Security invariants (same as ``LocalTrainer``):
- NO broker credentials, NO Redis, NO stream write capability.
- ``Authority.SHADOW_ONLY`` always — no promotion in the trainer.
- Deterministic given same seed + data (``deterministic=True``,
  ``num_threads=1`` in LightGBM params).
- Time/budget enforced: deadline breach raises ``TrainingFailure``.
- Training failure returns a safe terminal status (``TrainingFailure``),
  not a raw exception.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import pickle
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import unquote, urlparse

# Phase 8 / T-8.1: explicit column roles + task spec. Imported lazily-safe
# (dataset_manifest / training_manifest do not import real_trainer, so
# there is no circular import). These are OPTIONAL inputs — when None the
# trainer falls back to the legacy infer-by-dropping-names behaviour with
# a deprecation warning.
from quant_foundry.dataset_manifest import (
    ColumnRoles,
    FoldSpec,
    validate_column_roles,
)
from quant_foundry.runpod_training import (
    TrainingFailure,
    _container_digest_or_default,
    _git_sha_or_default,
    _lockfile_hash_or_default,
)
from quant_foundry.schemas import (
    ArtifactManifest,
    Authority,
    ModelDossier,
    RunPodTrainingRequest,
)
from quant_foundry.training_manifest import (
    ModelTaskSpec,
    validate_task_spec,
)

try:
    from fincept_core.storage import StorageBackend, get_storage_backend
except ImportError:  # pragma: no cover - fincept-core always present in-workspace
    StorageBackend = None  # type: ignore[assignment,misc]
    get_storage_backend = None  # type: ignore[assignment]


def _probe_gpu_model() -> str | None:
    """Probe the GPU model name via ``nvidia-smi``.

    Returns the first GPU's model name (e.g. ``"NVIDIA GeForce RTX 4090"``)
    or ``None`` if ``nvidia-smi`` is not available or no GPU is detected.
    This is the ground-truth GPU model for the artifact manifest's
    ``gpu_model`` field (Tier 1.3).
    """
    import subprocess

    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode != 0:
            return None
        name = proc.stdout.strip().splitlines()[0].strip() if proc.stdout.strip() else None
        return name or None
    except (FileNotFoundError, subprocess.TimeoutExpired, IndexError):
        return None


# ---------------------------------------------------------------------------
# Multi-backend dispatch (T-7.2 / T-7.3 integration)
# ---------------------------------------------------------------------------

#: Supported trainer backends. The default (``"lightgbm"``) preserves the
#: existing :class:`RealLightGBMTrainer` behaviour. ``"catboost"`` delegates
#: to :class:`quant_foundry.catboost_trainer.CatBoostTrainer` and
#: ``"xgboost"`` delegates to
#: :class:`quant_foundry.xgboost_trainer.XGBoostTrainer`.
#:
#: Tier 1.3: ``"xgboost_gpu"`` is a non-deterministic GPU backend that
#: routes through the same :class:`XGBoostTrainer` code path as
#: ``"xgboost"`` but sets ``device='cuda'`` in the XGBoost params (via
#: ``req.model_family == 'xgboost_gpu'``). GPU floating-point summation
#: order differs from CPU, so GPU training is flagged
#: ``non_deterministic`` in the artifact manifest. The RunPod handler's
#: ``_REAL_TRAINER_BACKEND_BY_FAMILY`` map also routes
#: ``model_family='xgboost_gpu'`` to the ``xgboost`` backend, so both
#: ``backend='xgboost_gpu'`` and ``backend='xgboost'`` (with
#: ``model_family='xgboost_gpu'``) reach ``_train_xgboost``.
#: Similarly, ``catboost_gpu`` routes to the ``catboost`` backend with
#: ``task_type='GPU'`` and ``determinism_status='non_deterministic'``.
TRAINER_BACKENDS: tuple[str, ...] = (
    "lightgbm",
    "catboost",
    "catboost_gpu",
    "xgboost",
    "xgboost_gpu",
)


# --- typed artifact result (Phase 1 / T-1.1) --------------------------------
#
# A typed, tamper-evident artifact result contract. This replaces the
# previous fragile ``getattr(result, "model_bytes", None)`` pattern in the
# RunPod handler: the trainer now produces a ``TypedArtifactResult`` with
# an explicit ``artifact_uri`` / ``artifact_sha256`` / ``artifact_size_bytes``
# triple (plus format/kind/loader provenance) so the handler can verify the
# artifact byte-for-byte and fail closed when an artifact is missing for a
# successful job.
#
# Design rules (matching the codebase Pydantic-v2 conventions):
# - ``frozen=True`` so the contract is immutable after construction.
# - ``artifact_sha256`` and ``artifact_size_bytes`` are ALWAYS required
#   (a successful training job that produces no artifact hash/size is a
#   contract violation — fail closed).
# - ``artifact_uri`` MAY be ``None`` for canary runs that keep tiny inline
#   ``model_bytes`` (never persisted). For real/research/production runs
#   the handler sets ``artifact_uri`` after writing the bytes to a volume
#   or object store.
# - ``model_bytes`` carries the inline artifact bytes. Canary tests keep
#   them inline; real runs persist them and clear the inline copy after
#   the handler has written + verified them.


@dataclass(frozen=True)
class TypedArtifactResult:
    """Typed, tamper-evident artifact result for a successful training job.

    Fields:
        artifact_id: stable id (``artifact:<sha16>``).
        artifact_uri: declared location of the persisted artifact. ``None``
            is allowed ONLY for canary runs that keep inline ``model_bytes``.
        artifact_sha256: SHA-256 hex (64 lowercase chars) of the artifact
            bytes. Always required.
        artifact_size_bytes: byte length of the artifact. Always required
            and must be > 0.
        artifact_format: serialisation format (``"pickle"``,
            ``"lightgbm-txt"``, ...).
        artifact_kind: artifact category (``"model"``).
        loader_family: loader used to reload the artifact
            (``"lightgbm"``, ``"local-stub"``).
        model_family: model family the artifact was trained for.
        dataset_manifest_hash: SHA-256 of the dataset manifest reference
            (binds the artifact to the data it was trained on).
        training_manifest_hash: SHA-256 of the training manifest content
            hash (from ``extra_constraints["manifest_content_hash"]``),
            or ``None`` when not forwarded.
        created_at: nanosecond epoch timestamp of artifact creation.
        model_bytes: inline artifact bytes. Present for canary runs and
            before the handler persists real runs; the handler clears
            this after a successful persist + verify.
    """

    artifact_id: str
    artifact_uri: str | None
    artifact_sha256: str
    artifact_size_bytes: int
    artifact_format: str
    artifact_kind: str
    loader_family: str
    model_family: str
    dataset_manifest_hash: str | None
    training_manifest_hash: str | None
    created_at: int
    model_bytes: bytes | None = None

    def __post_init__(self) -> None:
        # Validate at construction time so a malformed result fails fast
        # (fail closed) rather than silently propagating.
        if not self.artifact_id or not self.artifact_id.strip():
            raise ValueError("TypedArtifactResult.artifact_id must be non-empty")
        if not self.artifact_sha256 or not self.artifact_sha256.strip():
            raise ValueError(
                "TypedArtifactResult.artifact_sha256 must be non-empty "
                "(success callback cannot be built without artifact hash)"
            )
        if len(self.artifact_sha256) != 64 or not all(
            c in "0123456789abcdef" for c in self.artifact_sha256.lower()
        ):
            raise ValueError(
                "TypedArtifactResult.artifact_sha256 must be a 64-char "
                f"lowercase hex SHA-256; got {self.artifact_sha256!r}"
            )
        if self.artifact_size_bytes <= 0:
            raise ValueError(
                "TypedArtifactResult.artifact_size_bytes must be > 0 "
                "(success callback cannot be built without artifact size)"
            )
        if not self.artifact_format or not self.artifact_format.strip():
            raise ValueError("TypedArtifactResult.artifact_format must be non-empty")
        if not self.artifact_kind or not self.artifact_kind.strip():
            raise ValueError("TypedArtifactResult.artifact_kind must be non-empty")
        if not self.loader_family or not self.loader_family.strip():
            raise ValueError("TypedArtifactResult.loader_family must be non-empty")
        if not self.model_family or not self.model_family.strip():
            raise ValueError("TypedArtifactResult.model_family must be non-empty")
        if self.created_at < 0:
            raise ValueError("TypedArtifactResult.created_at must be >= 0")
        # Normalise sha to lowercase for stable comparisons.
        if self.artifact_sha256 != self.artifact_sha256.lower():
            object.__setattr__(self, "artifact_sha256", self.artifact_sha256.lower())

    def verify_bytes(self, data: bytes) -> bool:
        """Recompute the SHA-256 of ``data`` and compare to ``artifact_sha256``.

        Returns ``True`` iff the recomputed hash matches the declared
        hash. Used by the handler to detect artifact corruption / sha
        mismatch (acceptance criterion: artifact sha mismatch fails).
        """
        import hashlib

        return hashlib.sha256(data).hexdigest() == self.artifact_sha256


def _dataset_manifest_hash(ref: str) -> str:
    """Stable SHA-256 of the dataset manifest reference string."""
    return hashlib.sha256(ref.encode("utf-8")).hexdigest()


def _training_manifest_hash(req: RunPodTrainingRequest) -> str | None:
    """Resolve the training manifest content hash from ``extra_constraints``.

    The dispatch path forwards the manifest content hash via
    ``extra_constraints["manifest_content_hash"]`` (see
    :meth:`TrainingManifest.to_dispatch_request`). Returns ``None`` when
    not forwarded (e.g. ad-hoc requests not staged through a manifest).
    """
    raw = req.extra_constraints.get("manifest_content_hash")
    if raw and isinstance(raw, str) and raw.strip():
        return raw.strip().lower()
    return None


def build_artifact_result(
    *,
    artifact_id: str,
    model_bytes: bytes,
    model_family: str,
    req: RunPodTrainingRequest,
    artifact_uri: str | None = None,
    artifact_format: str = "pickle",
    artifact_kind: str = "model",
    loader_family: str = "lightgbm",
    created_at: int | None = None,
) -> TypedArtifactResult:
    """Build a :class:`TypedArtifactResult` from raw model bytes.

    Computes the SHA-256 + size from ``model_bytes`` (never from request
    inputs) and binds the dataset/training manifest hashes from the
    request. Raises ``ValueError`` (fail closed) when ``model_bytes`` is
    empty — a successful training job that produces no artifact bytes is
    a contract violation.
    """
    if not model_bytes:
        raise ValueError(
            "cannot build TypedArtifactResult without artifact bytes "
            "(successful training job produced no model artifact — fail closed)"
        )
    sha = hashlib.sha256(model_bytes).hexdigest()
    if created_at is None:
        created_at = time.time_ns()
    return TypedArtifactResult(
        artifact_id=artifact_id,
        artifact_uri=artifact_uri,
        artifact_sha256=sha,
        artifact_size_bytes=len(model_bytes),
        artifact_format=artifact_format,
        artifact_kind=artifact_kind,
        loader_family=loader_family,
        model_family=model_family,
        dataset_manifest_hash=_dataset_manifest_hash(req.dataset_manifest_ref),
        training_manifest_hash=_training_manifest_hash(req),
        created_at=created_at,
        model_bytes=model_bytes,
    )


# Canonical walk-forward fold math (single shared home — see
# ``fincept_core.datasets.cv``). Delegating here keeps Path B (RunPod real
# trainer) consistent with Path A (agents.gbm_predictor.train) and the
# backtester, and applies the purge gap that prevents forward-return label
# leakage between train and validation windows.
try:
    from fincept_core.datasets import make_folds as _make_folds
except ImportError:  # pragma: no cover - fincept-core always present in-workspace
    _make_folds = None  # type: ignore[assignment]


@dataclass
class RealLightGBMTrainer:
    """Real LightGBM trainer with walk-forward validation.

    Same interface as ``LocalTrainer``:
        ``train(req, *, deadline_ns) -> tuple[ArtifactManifest, ModelDossier]``

    ML dependencies are imported lazily inside ``train()`` so the
    ``quant_foundry`` package can be imported without ``lightgbm`` or
    ``numpy`` installed.

    Args:
        should_fail: if True, raise ``TrainingFailure`` on every ``train()``
            call (used to test the failure path).
        n_folds: number of walk-forward validation folds (expanding window).
        annualization_factor: square-root factor for Sharpe annualization
            (252 = daily, 52 = weekly, 12 = monthly).
    """

    should_fail: bool = False
    n_folds: int = 3
    # Tier 2.1: CV mode — "walk_forward" (expanding-window, default) or
    # "cpcv" (combinatorial purged cross-validation). When "cpcv", the
    # trainer uses ``make_cpcv_folds`` with ``cpcv_n_groups`` and
    # ``cpcv_n_val_groups`` and computes the real CSCV PBO.
    cv_mode: str = "walk_forward"
    cpcv_n_groups: int = 6
    cpcv_n_val_groups: int = 2
    # Default annualization factor for backward compat with callers that
    # never set ``bar_seconds``. The Sharpe is now annualized from
    # ``bar_seconds`` (read from ``extra_constraints``) when available —
    # see ``_resolve_periods_per_year``. This field is only used as the
    # fallback when no bar-frequency info is present on the request.
    annualization_factor: int = 252
    storage_backend: Any = None
    # --- Phase 8 / T-8.1: explicit column roles + task spec -------------
    # When set, the trainer uses ONLY ``column_roles.feature_columns`` as
    # features (never inferred by dropping a few names) and
    # ``column_roles.label_columns[0]`` (or ``task_spec.label_column``) as
    # the label. Excluded columns are fail-closed if they appear in the
    # feature set. When None, the trainer falls back to the legacy
    # infer-by-dropping-names behaviour and logs a deprecation warning.
    column_roles: ColumnRoles | None = None
    task_spec: ModelTaskSpec | None = None
    # --- T-7.2 / T-7.3 / T-8.4 integration fields -----------------------
    # ``backend`` selects the training backend: ``"lightgbm"`` (default,
    # existing behaviour), ``"catboost"`` (delegates to CatBoostTrainer),
    # or ``"xgboost"`` (delegates to XGBoostTrainer). The catboost/xgboost
    # backends REQUIRE ``column_roles`` + ``task_spec`` (fail-closed when
    # None) because those backends have no legacy infer-by-dropping-names
    # fallback.
    backend: str = "lightgbm"
    # ``fold_spec`` is the manifest-declared fold specification (T-8.4).
    # When set, the trainer consumes fold windows *exactly* as declared
    # (via ``consume_manifest_folds``) instead of re-deriving fold
    # boundaries from the data. When None, the trainer falls back to the
    # existing walk-forward fold generation (canary mode).
    fold_spec: FoldSpec | None = None
    # ``is_production`` enables the fail-closed guard: a production
    # manifest MUST declare a FoldSpec (no fallback to heuristic folds).
    is_production: bool = False
    # --- Tier 2.2: real Optuna trial count for honest Deflated Sharpe ---
    # The number of hyperparameter trials evaluated by Optuna (Tier 1.4).
    # The handler passes the real ``optuna_trial_count`` from the Optuna
    # tuning phase so the DSR's multiple-trials penalty reflects the
    # actual search breadth. Default 1 = no hyperparameter search at
    # this layer (backward compatible: DSR == single-trial deflation).
    trial_count: int = 1
    # --- Tier 2.7: checkpoint/resume for spot-fleet training -------------
    # When ``checkpoint_manager`` is set, the trainer saves a per-fold
    # checkpoint after each completed fold. When ``resume_from_fold`` is
    # set (0-based fold index), the trainer skips folds 0..resume_from_fold
    # and resumes training from resume_from_fold + 1. Both are wired by
    # the handler from ``req.checkpoint_dir`` / existing checkpoints.
    checkpoint_manager: Any = None
    resume_from_fold: int | None = None
    # --- Tier 2.3: transient label map for multiclass metrics -----------
    # Set by _walk_forward_validate / _cpcv_validate during the fold loop
    # so _compute_multiclass_metrics can reverse-map class indices back
    # to original label values (e.g. {0,1,2} → {-1,0,+1}) for position
    # / returns computation. Per-job transient state (handler creates a
    # fresh trainer per job).
    _label_map: dict[int, int] | None = None
    # --- Phase 1 / T-1.1: typed artifact result -------------------------
    # After a successful ``train()`` call, the typed artifact result +
    # raw model bytes are stashed here so the RunPod handler can read
    # them through a typed field instead of the fragile
    # ``getattr(result, "model_bytes", None)`` pattern. The handler
    # creates a fresh trainer per job, so this per-instance state is
    # safe (no cross-job leakage).
    last_artifact_result: TypedArtifactResult | None = None
    last_model_bytes: bytes | None = None
    # C1: selfcheck sample (feature rows) for the handler's bundle selfcheck.
    last_selfcheck_features: list[list[float]] | None = None

    # --- public API ------------------------------------------------------

    def train(
        self,
        req: RunPodTrainingRequest,
        *,
        deadline_ns: int,
    ) -> tuple[ArtifactManifest, ModelDossier]:
        """Train a real LightGBM model with walk-forward validation.

        Returns ``(artifact_manifest, dossier)``.

        Raises ``TrainingFailure`` on deadline breach, missing dependencies,
        insufficient data, or if ``should_fail`` is set.
        """
        if self.should_fail:
            raise TrainingFailure(
                error_code="training_error",
                error_summary="real trainer injected failure (should_fail=True)",
            )

        if time.time_ns() >= deadline_ns:
            raise TrainingFailure(
                error_code="timeout",
                error_summary="training deadline breached before work started",
            )

        # --- T-7.2 / T-7.3 / T-8.4 integration ---------------------------
        # Validate the backend string.
        if self.backend not in TRAINER_BACKENDS:
            raise TrainingFailure(
                error_code="invalid_backend",
                error_summary=(
                    f"unknown backend {self.backend!r}; supported: {list(TRAINER_BACKENDS)}"
                ),
            )

        # Production fail-closed (T-8.4): a production manifest MUST
        # declare a FoldSpec — the contract of record for fold
        # boundaries. Canary/research runs may fall back to the
        # heuristic walk-forward fold generation.
        if self.is_production:
            self._require_fold_spec_for_production()

        # Multi-backend dispatch: catboost / xgboost backends have their
        # own dependency checks + training paths, so we delegate before
        # the lightgbm-specific dependency checks below.
        if self.backend != "lightgbm":
            return self._train_with_backend(req, deadline_ns=deadline_ns)

        if importlib.util.find_spec("lightgbm") is None:
            raise TrainingFailure(
                error_code="missing_dependency",
                error_summary="ML dependency not available: lightgbm",
            )
        if importlib.util.find_spec("numpy") is None:
            raise TrainingFailure(
                error_code="missing_dependency",
                error_summary="ML dependency not available: numpy",
            )

        # Phase 8 / T-8.1: validate explicit column roles + task spec.
        # When column_roles is provided, the trainer uses ONLY the
        # declared feature columns (never inferred). When None, fall back
        # to the legacy infer-by-dropping-names behaviour with a
        # deprecation warning.
        self._validate_column_roles_and_task_spec()

        # T-8.4: when a FoldSpec is present, load the dataset as a
        # dataframe so ``consume_manifest_folds`` can assign rows to
        # folds, then extract numpy arrays in original row order. When
        # no FoldSpec, use the existing ``_load_dataset`` path (canary
        # mode — heuristic walk-forward folds).
        fold_assignment = None
        if self.fold_spec is not None:
            X, y, timestamps, weights, groups, fold_assignment = self._load_dataset_with_folds(
                req.dataset_manifest_ref
            )
        else:
            X, y, timestamps, weights, groups = self._load_dataset(
                req.dataset_manifest_ref,
            )

        if time.time_ns() >= deadline_ns:
            raise TrainingFailure(
                error_code="timeout",
                error_summary="training deadline breached after dataset load",
            )

        seed = req.random_seed if req.random_seed is not None else 0

        # Tier 2.1: dispatch to CPCV validation when cv_mode="cpcv".
        # CPCV is not compatible with manifest fold_assignment (which
        # specifies exact fold windows); in that case we fall back to
        # walk-forward.
        if self.cv_mode == "cpcv" and fold_assignment is None:
            metrics = self._cpcv_validate(
                X,
                y,
                timestamps,
                seed,
                deadline_ns,
                req,
                weights=weights,
                groups=groups,
            )
        else:
            metrics = self._walk_forward_validate(
                X,
                y,
                timestamps,
                seed,
                deadline_ns,
                req,
                weights=weights,
                groups=groups,
                fold_assignment=fold_assignment,
            )

        # T-8.2: rank metrics integration. When the task is ranking and
        # groups are available, compute the cross-sectional rank metrics
        # from the out-of-sample predictions collected during
        # walk-forward validation.
        rank_report = self._maybe_compute_rank_metrics(metrics, groups, timestamps)
        if rank_report is not None:
            metrics["rank_report"] = rank_report

        if time.time_ns() >= deadline_ns:
            raise TrainingFailure(
                error_code="timeout",
                error_summary="training deadline breached after validation",
            )

        final_model = self._train_final_model(X, y, seed, req, weights=weights)

        # Tier 2.3b: meta-labeling — train a secondary binary classifier
        # that decides whether to act on the primary model's signal.
        # The meta-model uses the primary model's predictions on the full
        # dataset as an additional feature. The meta-model's own
        # walk-forward validation provides the OOS estimate.
        meta_model = None
        meta_metrics: dict[str, Any] = {}
        if self._is_meta_labeling():
            meta_model, meta_metrics = self._train_meta_model(X, y, final_model, seed, req)

        # C1: Serialize the model(s) as a ModelBundle v1 archive (zip).
        # New training writes only ModelBundle v1. The bundle carries
        # bundle_manifest.json listing every member + sha256, the primary
        # model, and (for meta-labeled) the meta model. load_bundle()
        # verifies member hashes before scoring. Legacy bare LightGBM
        # pickle is load-only compatibility (handled in bundle_io).
        from quant_foundry.bundle_io import write_bundle as _write_bundle

        n_features = int(X.shape[1])
        n_rows = int(X.shape[0])
        # C1: feature_schema_hash must be path-independent for reproducibility.
        # Hash the feature count + label type, not the dataset path.
        feature_schema_hash = hashlib.sha256(
            f"n_features={n_features}".encode(),
        ).hexdigest()[:16]
        label_type = (
            "meta"
            if meta_model is not None
            else ("multiclass" if self._is_multiclass() else "binary")
        )
        label_schema_hash = hashlib.sha256(
            f"label={label_type}".encode(),
        ).hexdigest()[:16]
        now_ns = time.time_ns()

        # Resolve feature names: prefer column_roles, else generic.
        if self.column_roles is not None and self.column_roles.feature_columns:
            feature_names = list(self.column_roles.feature_columns)
        else:
            feature_names = [f"f{i}" for i in range(n_features)]

        meta_label_config = None
        if self.task_spec is not None:
            meta_label_config = self.task_spec.meta_label_config

        # C1: use a deterministic created_at_ns for the bundle manifest
        # so the bundle bytes (and thus artifact sha256) are reproducible
        # given the same seed + data. The wall-clock now_ns is still used
        # for the ArtifactManifest.created_at_ns field (metadata only).
        bundle_created_ns = int(req.random_seed) if req.random_seed is not None else 0

        model_bytes = _write_bundle(
            primary_model=final_model,
            meta_model=meta_model,
            feature_names=feature_names,
            feature_schema_hash=feature_schema_hash,
            label_schema_hash=label_schema_hash,
            model_family=req.model_family,
            label_map={str(k): v for k, v in self._label_map.items()} if self._label_map else None,
            meta_label_config=meta_label_config,
            created_at_ns=bundle_created_ns,
        )
        sha256 = hashlib.sha256(model_bytes).hexdigest()
        size_bytes = len(model_bytes)

        # Stash a selfcheck sample (a few feature rows) so the handler
        # can run the bundle selfcheck against the final artifact bytes.
        # Uses the first min(10, n_rows) rows of the training features.
        import numpy as _np

        self.last_selfcheck_features = _np.asarray(X[: min(10, n_rows)]).tolist()

        artifact_id = f"artifact:{sha256[:16]}"
        artifact = ArtifactManifest(
            artifact_id=artifact_id,
            sha256=sha256,
            size_bytes=size_bytes,
            uri=None,
            model_family=req.model_family,
            created_at_ns=now_ns,
            feature_schema_hash=feature_schema_hash,
            label_schema_hash=label_schema_hash,
            code_git_sha=_git_sha_or_default(),
            lockfile_hash=_lockfile_hash_or_default(),
            container_image_digest=_container_digest_or_default(),
        )

        # Phase 1 / T-1.1: build the typed artifact result from the real
        # model bytes (sha/size computed from bytes, never from request
        # inputs) and stash it on the instance so the RunPod handler can
        # read it through a typed field. ``artifact_uri`` is left None
        # here — the handler sets it after persisting the bytes to a
        # volume / object store. Fail closed: if the model bytes are
        # empty (a successful job with no artifact), build_artifact_result
        # raises ValueError, which we translate to a TrainingFailure.
        try:
            typed_result = build_artifact_result(
                artifact_id=artifact_id,
                model_bytes=model_bytes,
                model_family=req.model_family,
                req=req,
                artifact_uri=None,
                artifact_format="bundle",
                artifact_kind="model",
                loader_family="lightgbm",
                created_at=now_ns,
            )
        except ValueError as exc:
            raise TrainingFailure(
                error_code="artifact_missing",
                error_summary=(
                    f"successful training produced no artifact bytes (fail closed): {exc}"
                ),
            ) from exc
        # Stash for the handler (mutable dataclass — safe per-job).
        self.last_artifact_result = typed_result
        self.last_model_bytes = model_bytes

        dossier = ModelDossier(
            model_id=f"model:{req.job_id}",
            artifact_manifest_id=artifact.artifact_id,
            dataset_manifest_id=req.dataset_manifest_ref,
            code_git_sha=artifact.code_git_sha or "unknown",
            lockfile_hash=artifact.lockfile_hash or "unknown",
            container_image_digest=artifact.container_image_digest or "unknown",
            random_seed=req.random_seed,
            hardware_class=req.hardware_class,
            training_metrics=metrics["training_metrics"],
            pbo=metrics["pbo"],
            deflated_sharpe=metrics["deflated_sharpe"],
            authority=Authority.SHADOW_ONLY,
            metadata={
                "model_family": req.model_family,
                "trainer": "real_lightgbm",
                "backend": self.backend,
                "n_features": str(n_features),
                "n_rows": str(n_rows),
                "n_folds": str(self.n_folds),
                "brier_score": str(metrics["brier_score"]),
                "win_rate": str(metrics["win_rate"]),
                "max_drawdown": str(metrics["max_drawdown"]),
                "sharpe_ratio": str(metrics["sharpe_ratio"]),
                "avg_best_iteration": str(metrics.get("avg_best_iteration", "n/a")),
                "fold_best_iterations": str(metrics.get("fold_best_iterations", [])),
                # F3: record the PBO/DSR method so an operator inspecting
                # the dossier knows these are heuristics, not the academic
                # Bailey & Lopez de Prado figures (the tournament computes
                # the real DSR via significance.deflated_sharpe_ratio).
                "pbo_method": metrics.get("pbo_method", "fold_overfit_ratio"),
                "deflated_sharpe_method": metrics.get(
                    "deflated_sharpe_method",
                    "sharpe_times_1_minus_fold_overfit_ratio",
                ),
                # T-8.4: record whether manifest folds or heuristic folds
                # were used.
                "fold_source": "manifest" if fold_assignment is not None else "heuristic",
                # T-8.2: rank metrics (present only for ranking tasks).
                "has_rank_report": str(rank_report is not None),
                # Tier 2.1/2.2: DSR + PBO detail fields for transparency.
                "deflated_sharpe_raw": str(metrics.get("deflated_sharpe_raw", "")),
                "deflated_sharpe_trial_count": str(metrics.get("deflated_sharpe_trial_count", "")),
                "deflated_sharpe_skew": str(metrics.get("deflated_sharpe_skew", "")),
                "deflated_sharpe_kurtosis": str(metrics.get("deflated_sharpe_kurtosis", "")),
                "deflated_sharpe_multiple_trials_penalty": str(
                    metrics.get("deflated_sharpe_multiple_trials_penalty", "")
                ),
                "deflated_sharpe_non_normality_penalty": str(
                    metrics.get("deflated_sharpe_non_normality_penalty", "")
                ),
                "pbo_logit": str(metrics.get("pbo_logit", "")),
                "pbo_n_combinations": str(metrics.get("pbo_n_combinations", "")),
                "pbo_flagged": str(metrics.get("pbo_flagged", "")),
                # Tier 2.1: CPCV mode metadata.
                "cv_mode": str(metrics.get("cv_mode", "walk_forward")),
                "cpcv_n_groups": str(metrics.get("cpcv_n_groups", "")),
                "cpcv_n_val_groups": str(metrics.get("cpcv_n_val_groups", "")),
                "cpcv_n_folds": str(metrics.get("cpcv_n_folds", "")),
                # Tier 2.3: triple-barrier label config (when set).
                "barrier_config": str(self.task_spec.barrier_config if self.task_spec else None),
                # Tier 2.3b: meta-labeling config + meta-model metrics.
                "meta_label_config": str(
                    self.task_spec.meta_label_config if self.task_spec else None
                ),
                "meta_accuracy": str(meta_metrics.get("meta_accuracy", "")),
                "meta_logloss": str(meta_metrics.get("meta_logloss", "")),
                "meta_brier_score": str(meta_metrics.get("meta_brier_score", "")),
                "meta_n_folds": str(meta_metrics.get("meta_n_folds", "")),
                "meta_positive_rate": str(meta_metrics.get("meta_positive_rate", "")),
                "has_meta_model": str(meta_model is not None),
                # Tier 2.5: execution-aware (net-of-cost) metrics.
                # The promotion gate should use sharpe_net, not
                # sharpe_ratio (which is frictionless/gross).
                "sharpe_net": str(metrics.get("sharpe_net", "")),
                "max_drawdown_net": str(metrics.get("max_drawdown_net", "")),
                "win_rate_net": str(metrics.get("win_rate_net", "")),
                "turnover": str(metrics.get("turnover", "")),
                "total_cost_bps": str(metrics.get("total_cost_bps", "")),
                "cost_model_version": str(metrics.get("cost_model_version", "")),
            },
        )

        return artifact, dossier

    # --- dataset loading -------------------------------------------------

    def _validate_column_roles_and_task_spec(self) -> None:
        """Validate ``column_roles`` and ``task_spec`` (Phase 8 / T-8.1).

        Fail-closed checks (raise ``TrainingFailure``):
        - If ``column_roles`` is set but ``feature_columns`` is empty.
        - If any excluded column appears in the feature set (leakage).
        - If ``task_spec`` is set and the label column is missing from
          ``column_roles.label_columns``.
        - For ranking tasks: ``group_column`` must be set on both the
          task spec and the column roles.

        When ``column_roles`` is None, log a deprecation warning and fall
        back to the legacy infer-by-dropping-names behaviour (backward
        compat).
        """
        import warnings

        if self.column_roles is None:
            # Backward compat: no explicit column roles. The legacy
            # loaders infer features by dropping the label + timestamp
            # columns. Emit a deprecation warning so callers migrate to
            # explicit ColumnRoles.
            warnings.warn(
                "RealLightGBMTrainer was constructed without column_roles; "
                "falling back to legacy infer-by-dropping-names feature "
                "selection. This is deprecated — pass an explicit "
                "ColumnRoles so features are declared, never inferred, "
                "and leakage/audit columns are excluded.",
                DeprecationWarning,
                stacklevel=2,
            )
            return

        # Fail-closed: feature_columns must be non-empty (already enforced
        # by the Pydantic model, but re-check here for defence in depth).
        if not self.column_roles.feature_columns:
            raise TrainingFailure(
                error_code="invalid_column_roles",
                error_summary=(
                    "column_roles.feature_columns is empty — the trainer "
                    "requires explicit feature columns (never inferred)"
                ),
            )

        # Fail-closed: no excluded column may appear in the feature set
        # (leakage prevention). The Pydantic model already enforces this
        # at construction, but re-check here so a mutated / stale roles
        # object is caught at train time.
        feature_set = set(self.column_roles.feature_columns)
        excluded_set = set(self.column_roles.excluded_columns)
        overlap = feature_set & excluded_set
        if overlap:
            raise TrainingFailure(
                error_code="leakage_column_in_features",
                error_summary=(
                    "excluded (leakage/audit) columns must never appear in "
                    f"feature_columns: {sorted(overlap)!r} are declared "
                    "both as features and excluded"
                ),
            )

        # Fail-closed: label columns must not be features.
        label_set = set(self.column_roles.label_columns)
        lf_overlap = feature_set & label_set
        if lf_overlap:
            raise TrainingFailure(
                error_code="label_in_features",
                error_summary=(
                    f"label columns must not appear in feature_columns: {sorted(lf_overlap)!r}"
                ),
            )

        # If a task spec is provided, validate it against the column roles.
        if self.task_spec is not None:
            verdict = validate_task_spec(self.task_spec, self.column_roles)
            if not verdict.passed:
                raise TrainingFailure(
                    error_code="invalid_task_spec",
                    error_summary=("task spec validation failed: " + "; ".join(verdict.errors)),
                )
            # Fail-closed: ranking without group column.
            if self.task_spec.task_type == "ranking":
                if not self.task_spec.group_column:
                    raise TrainingFailure(
                        error_code="ranking_without_group",
                        error_summary=(
                            "ranking task requires a group_column "
                            "(ranking request without group id fails)"
                        ),
                    )
                if self.column_roles.group_column is None:
                    raise TrainingFailure(
                        error_code="ranking_without_group",
                        error_summary=(
                            "ranking task requires column_roles.group_column "
                            "to be set (the group-id column must be declared)"
                        ),
                    )

    def _resolve_path(self, ref: str) -> Path:
        """Resolve a dataset reference (file://, s3://, http(s):// URI, or plain path) to a Path.

        For ``s3://`` URIs, the configured ``storage_backend`` (or the factory
        singleton) is used to download the object to a temp file, which is
        returned. For ``http://`` and ``https://`` URIs, the file is downloaded
        to a temp file via ``urllib.request``. For ``file://`` URIs and bare
        paths, behavior is unchanged (backward compat).
        """
        parsed = urlparse(ref)
        # A single-letter scheme is a Windows drive letter (e.g. "C:\\path"),
        # not a real URI scheme. Treat it as a bare local path.
        if len(parsed.scheme) == 1:
            return Path(ref)
        if parsed.scheme == "file":
            path = unquote(parsed.path)
            if os.name == "nt" and len(path) > 2 and path[0] == "/" and path[2] == ":":
                path = path[1:]
            return Path(path)
        elif parsed.scheme == "":
            return Path(ref)
        elif parsed.scheme in ("http", "https"):
            import tempfile
            import urllib.request

            suffix = ""
            if parsed.path.endswith(".parquet"):
                suffix = ".parquet"
            elif parsed.path.endswith(".csv"):
                suffix = ".csv"
            # C4C: use mkstemp (safe) instead of mktemp (racy, deprecated).
            fd, tmp_name = tempfile.mkstemp(prefix="qf_http_", suffix=suffix)
            os.close(fd)  # close fd; urlretrieve will write to the path
            tmp_path = Path(tmp_name)
            try:
                urllib.request.urlretrieve(ref, str(tmp_path))
            except Exception as exc:
                raise TrainingFailure(
                    error_code="dataset_download_failed",
                    error_summary=f"failed to download HTTP dataset {ref!r}: {exc}",
                ) from exc
            return tmp_path
        elif parsed.scheme == "s3":
            backend = self.storage_backend
            if backend is None and get_storage_backend is not None:
                try:
                    backend = get_storage_backend()
                except Exception as exc:
                    raise TrainingFailure(
                        error_code="unsupported_uri",
                        error_summary=f"no storage backend available for s3 dataset: {exc}",
                    ) from exc
            if backend is None:
                raise TrainingFailure(
                    error_code="unsupported_uri",
                    error_summary=f"s3 dataset loading requires a storage backend: {ref}",
                )
            try:
                tmp_path = backend.download_to_temp(ref)
            except TrainingFailure:
                raise
            except Exception as exc:
                raise TrainingFailure(
                    error_code="unsupported_uri",
                    error_summary=f"failed to fetch s3 dataset {ref!r}: {exc}",
                ) from exc
            return Path(tmp_path)
        else:
            raise TrainingFailure(
                error_code="unsupported_uri",
                error_summary=f"unsupported URI scheme: {parsed.scheme!r}",
            )

    def _load_dataset(
        self,
        ref: str,
    ) -> tuple[Any, Any, Any, Any, Any]:
        """Load dataset from a URI.

        Returns ``(X, y, timestamps, weights, groups)`` where ``weights``
        and ``groups`` are ``None`` when no ``column_roles`` is set or the
        corresponding columns are not declared.
        """
        path = self._resolve_path(ref)
        if not path.exists():
            raise TrainingFailure(
                error_code="dataset_not_found",
                error_summary=f"dataset file not found: {path}",
            )

        ext = path.suffix.lower()
        if ext == ".parquet":
            return self._load_parquet(path)
        elif ext == ".csv":
            return self._load_csv(path)
        else:
            raise TrainingFailure(
                error_code="unsupported_format",
                error_summary=f"unsupported dataset format: {ext} (expected .parquet or .csv)",
            )

    def _load_parquet(self, path: Path) -> tuple[Any, Any, Any, Any, Any]:
        """Load a parquet file. Requires pyarrow or pandas (lazy import).

        When ``column_roles`` is set, uses ONLY the declared
        ``feature_columns`` as features (never inferred by dropping names)
        and ``label_columns[0]`` (or ``task_spec.label_column``) as the
        label. Extracts weights/groups from the declared columns when
        present.
        """
        import numpy as np

        try:
            import pyarrow.parquet as pq

            table = pq.read_table(str(path))  # type: ignore[no-untyped-call]  # pyarrow read_table lacks type stubs
            columns = table.column_names
            data = table.to_pydict()
        except ImportError:
            try:
                import pandas as pd

                df = pd.read_parquet(str(path))
                columns = list(df.columns)
                data = {col: df[col].tolist() for col in columns}
            except ImportError:
                raise TrainingFailure(
                    error_code="missing_dependency",
                    error_summary="neither pyarrow nor pandas available for parquet loading",
                ) from None

        available = set(columns)

        # --- Phase 8 / T-8.1: explicit column roles --------------------
        if self.column_roles is not None:
            roles = self.column_roles
            # Validate declared columns exist in the dataset.
            verdict = validate_column_roles(roles, available)
            if not verdict.passed:
                raise TrainingFailure(
                    error_code="invalid_column_roles",
                    error_summary=("column roles validation failed: " + "; ".join(verdict.errors)),
                )
            # Label: prefer task_spec.label_column, else roles.primary_label.
            if self.task_spec is not None and self.task_spec.label_column:
                label_col = self.task_spec.label_column
            else:
                label_col = roles.primary_label
            if label_col not in available:
                raise TrainingFailure(
                    error_code="missing_label",
                    error_summary=(
                        f"label column {label_col!r} not found in dataset "
                        f"columns {sorted(available)!r} (missing label fails)"
                    ),
                )
            # Features: ONLY the declared feature columns.
            feature_cols = list(roles.feature_columns)
            for fc in feature_cols:
                if fc not in available:
                    raise TrainingFailure(
                        error_code="missing_feature",
                        error_summary=(
                            f"feature column {fc!r} not found in dataset "
                            f"columns {sorted(available)!r}"
                        ),
                    )
            # Timestamp.
            ts_col = roles.timestamp_column
            # Weights.
            weights = None
            if roles.weight_column is not None:
                if roles.weight_column not in available:
                    raise TrainingFailure(
                        error_code="missing_weight_column",
                        error_summary=(
                            f"weight column {roles.weight_column!r} not found in dataset columns"
                        ),
                    )
                weights = np.array(data[roles.weight_column], dtype=np.float64)
            # Groups.
            groups = None
            if roles.group_column is not None:
                if roles.group_column not in available:
                    raise TrainingFailure(
                        error_code="missing_group_column",
                        error_summary=(
                            f"group column {roles.group_column!r} not found in dataset columns"
                        ),
                    )
                groups = np.array(data[roles.group_column])
        else:
            # Legacy: infer by dropping label + timestamp.
            label_col = "label" if "label" in columns else columns[-1]
            ts_col = None
            for candidate in ("timestamp", "decision_time", "ts", "event_ts"):
                if candidate in columns:
                    ts_col = candidate
                    break
            feature_cols = [c for c in columns if c != label_col and c != ts_col]
            weights = None
            groups = None

        y = np.array(data[label_col], dtype=np.float64)
        X = np.column_stack(
            [np.array(data[c], dtype=np.float64) for c in feature_cols],
        )

        if ts_col is not None and ts_col in available:
            timestamps = np.array(data[ts_col], dtype=np.int64)
        else:
            timestamps = np.arange(len(y), dtype=np.int64)

        return X, y, timestamps, weights, groups

    def _load_csv(self, path: Path) -> tuple[Any, Any, Any, Any, Any]:
        """Load a CSV file using numpy.

        Expected layout: first column = timestamp, last column = label,
        middle columns = features. A header row is required.

        When ``column_roles`` is set, the header row is read to map column
        names to indices, and ONLY the declared ``feature_columns`` are
        used as features (never inferred by dropping names).
        """
        import numpy as np

        # Read the header to get column names (needed for column-roles
        # mapping). The header is the first non-empty line.
        with open(str(path), encoding="utf-8", errors="replace") as fh:
            header_line = fh.readline().strip()
        header_cols = [c.strip() for c in header_line.split(",")] if header_line else []

        data = np.genfromtxt(str(path), delimiter=",", skip_header=1, dtype=float)
        if data.ndim == 1:
            data = data.reshape(1, -1)

        if data.shape[1] < 3:
            raise TrainingFailure(
                error_code="insufficient_features",
                error_summary=(
                    f"CSV must have at least 3 columns (timestamp, features, "
                    f"label); got {data.shape[1]}"
                ),
            )

        # --- Phase 8 / T-8.1: explicit column roles --------------------
        if self.column_roles is not None and header_cols:
            roles = self.column_roles
            available = set(header_cols)
            verdict = validate_column_roles(roles, available)
            if not verdict.passed:
                raise TrainingFailure(
                    error_code="invalid_column_roles",
                    error_summary=("column roles validation failed: " + "; ".join(verdict.errors)),
                )
            col_index = {name: i for i, name in enumerate(header_cols)}
            # Label.
            if self.task_spec is not None and self.task_spec.label_column:
                label_col = self.task_spec.label_column
            else:
                label_col = roles.primary_label
            if label_col not in col_index:
                raise TrainingFailure(
                    error_code="missing_label",
                    error_summary=(
                        f"label column {label_col!r} not found in CSV "
                        f"header {header_cols!r} (missing label fails)"
                    ),
                )
            y = data[:, col_index[label_col]].astype(np.float64)
            # Features: ONLY the declared feature columns.
            feature_indices = []
            for fc in roles.feature_columns:
                if fc not in col_index:
                    raise TrainingFailure(
                        error_code="missing_feature",
                        error_summary=(
                            f"feature column {fc!r} not found in CSV header {header_cols!r}"
                        ),
                    )
                feature_indices.append(col_index[fc])
            X = data[:, feature_indices].astype(np.float64)
            if X.ndim == 1:
                X = X.reshape(-1, 1)
            # Timestamp.
            if roles.timestamp_column and roles.timestamp_column in col_index:
                timestamps = data[:, col_index[roles.timestamp_column]].astype(np.int64)
            else:
                timestamps = np.arange(len(y), dtype=np.int64)
            # Weights.
            weights = None
            if roles.weight_column is not None:
                if roles.weight_column not in col_index:
                    raise TrainingFailure(
                        error_code="missing_weight_column",
                        error_summary=(
                            f"weight column {roles.weight_column!r} not found in CSV header"
                        ),
                    )
                weights = data[:, col_index[roles.weight_column]].astype(np.float64)
            # Groups.
            groups = None
            if roles.group_column is not None:
                if roles.group_column not in col_index:
                    raise TrainingFailure(
                        error_code="missing_group_column",
                        error_summary=(
                            f"group column {roles.group_column!r} not found in CSV header"
                        ),
                    )
                groups = data[:, col_index[roles.group_column]]
        else:
            # Legacy: first column = timestamp, last = label, middle = features.
            timestamps = data[:, 0].astype(np.int64)
            y = data[:, -1].astype(np.float64)
            X = data[:, 1:-1].astype(np.float64)
            if X.ndim == 1:
                X = X.reshape(-1, 1)
            weights = None
            groups = None

        return X, y, timestamps, weights, groups

    # --- LightGBM params -------------------------------------------------

    def _build_lgb_params(self, seed: int, req: RunPodTrainingRequest) -> dict[str, Any]:
        """Build LightGBM parameters from request search space + defaults.

        Tier 2.3: when ``task_spec.task_type == "multiclass"``, the
        objective is set to ``"multiclass"`` with ``num_class`` from the
        data (set by the caller after inspecting labels). This supports
        triple-barrier labels (+1/-1/0 mapped to {0,1,2}).
        """
        # Tier 2.3: multiclass objective for triple-barrier labels.
        is_multiclass = self.task_spec is not None and self.task_spec.task_type == "multiclass"
        params: dict[str, Any] = {
            "objective": "multiclass" if is_multiclass else "binary",
            "metric": "multi_logloss" if is_multiclass else "binary_logloss",
            "verbosity": -1,
            "seed": seed,
            "deterministic": True,
            "num_threads": 1,
            "num_leaves": 31,
            "learning_rate": 0.05,
            "feature_fraction": 0.9,
            "bagging_fraction": 0.8,
            "bagging_freq": 5,
            "min_data_in_leaf": 5,
            "force_col_wise": True,
            # Regularization defaults (can be overridden via search_space)
            "lambda_l1": 0.0,
            "lambda_l2": 0.0,
            "min_split_gain": 0.0,
            "path_smooth": 0.0,
            "extra_trees": False,
        }

        ss = req.search_space
        if ss.get("num_leaves"):
            params["num_leaves"] = int(ss["num_leaves"][0])
        if ss.get("learning_rate"):
            params["learning_rate"] = float(ss["learning_rate"][0])
        if ss.get("max_depth"):
            params["max_depth"] = int(ss["max_depth"][0])
        if ss.get("min_data_in_leaf"):
            params["min_data_in_leaf"] = int(ss["min_data_in_leaf"][0])
        if ss.get("feature_fraction"):
            params["feature_fraction"] = float(ss["feature_fraction"][0])
        if ss.get("bagging_fraction"):
            params["bagging_fraction"] = float(ss["bagging_fraction"][0])
        if ss.get("bagging_freq"):
            params["bagging_freq"] = int(ss["bagging_freq"][0])
        if ss.get("lambda_l1"):
            params["lambda_l1"] = float(ss["lambda_l1"][0])
        if ss.get("lambda_l2"):
            params["lambda_l2"] = float(ss["lambda_l2"][0])
        if ss.get("min_split_gain"):
            params["min_split_gain"] = float(ss["min_split_gain"][0])
        if ss.get("path_smooth"):
            params["path_smooth"] = float(ss["path_smooth"][0])
        if ss.get("extra_trees"):
            params["extra_trees"] = bool(ss["extra_trees"][0])
        if ss.get("num_threads"):
            params["num_threads"] = int(ss["num_threads"][0])

        return params

    def _get_n_estimators(self, req: RunPodTrainingRequest) -> int:
        ss = req.search_space
        if ss.get("n_estimators"):
            return int(ss["n_estimators"][0])
        return 100

    # --- bar frequency + annualization (F1 fix) -------------------------

    def _resolve_bar_seconds(self, extra_constraints: dict[str, str]) -> int:
        """Resolve the bar frequency in seconds from ``extra_constraints``.

        Defaults to 60 (1-minute bars), matching the platform's primary
        use case (crypto microstructure on 1-minute bars). The
        ``RunPodTrainingRequest`` schema has no dedicated ``bar_seconds``
        field, so it is carried through ``extra_constraints`` by the
        dispatch path (see ``local_training_dispatch.py``).
        """
        raw = extra_constraints.get("bar_seconds")
        if raw is None:
            return 60
        try:
            v = int(raw)
        except (TypeError, ValueError):
            return 60
        return v if v > 0 else 60

    def _resolve_periods_per_year(self, extra_constraints: dict[str, str]) -> int:
        """Resolve the annualization factor (periods per year) for Sharpe.

        Resolution order:
        1. Explicit ``annualization_periods_per_year`` in
           ``extra_constraints`` (e.g. ``252`` for US-equity daily bars,
           ``12`` for monthly). Use this for non-24/7 markets.
        2. Derived from ``bar_seconds`` assuming a 24/7 market (the
           platform default — crypto): ``seconds_per_year / bar_seconds``.
        3. Fallback to the trainer's ``annualization_factor`` field
           (default 252) for backward compat with callers that never
           set bar-frequency info.

        Before this fix the trainer hardcoded ``sqrt(252)`` on per-bar
        returns, understating Sharpe by ~45x for 1-minute crypto bars
        (see docs/TRAINING_ANALYSIS.md finding F1).
        """
        explicit = extra_constraints.get("annualization_periods_per_year")
        if explicit is not None:
            try:
                v = int(explicit)
                if v > 0:
                    return v
            except (TypeError, ValueError):
                pass
        bar_seconds = self._resolve_bar_seconds(extra_constraints)
        seconds_per_year = 365 * 24 * 60 * 60  # 31_536_000 (24/7 market)
        return seconds_per_year // bar_seconds

    # --- walk-forward fold math (F2 + F4 fix) ---------------------------

    def _resolve_horizon_bars(self, extra_constraints: dict[str, str]) -> int:
        """Resolve the forward-return horizon in bars from ``extra_constraints``.

        Defaults to 15, matching the platform default
        (``agents.gbm_predictor.train --horizon-bars 15``).
        """
        raw = extra_constraints.get("horizon_bars")
        if raw is None:
            return 15
        try:
            v = int(raw)
        except (TypeError, ValueError):
            return 15
        return v if v > 0 else 15

    def _resolve_purge_bars(
        self,
        horizon_bars: int,
        extra_constraints: dict[str, str],
    ) -> int:
        """Resolve the purge gap in bars between train end and val start.

        The purge gap prevents forward-return label leakage: a training
        row at time ``t`` has a label that depends on prices at
        ``t + horizon_bars``, so the last ``horizon_bars`` training rows
        have labels that overlap the validation window unless a gap is
        enforced.

        Resolution order:
        1. Explicit ``purge_bars`` in ``extra_constraints``.
        2. Default to ``horizon_bars`` (matches Path A behavior where
           ``--purge-bars -1`` means "use --horizon-bars").
        """
        raw = extra_constraints.get("purge_bars")
        if raw is None:
            return horizon_bars
        try:
            v = int(raw)
        except (TypeError, ValueError):
            return horizon_bars
        return v if v >= 0 else horizon_bars

    def _build_walk_forward_folds(
        self,
        n_rows: int,
        purge_bars: int,
        n_folds: int,
    ) -> list[Any]:
        """Build expanding-window walk-forward folds with a purge gap.

        Delegates to the canonical ``fincept_core.datasets.cv.make_folds``
        so Path B (RunPod real trainer) and Path A
        (``agents.gbm_predictor.train``) share identical fold math. This
        fixes both:

        - F2: the purge gap (``val_start - train_end >= purge_bars``) is
          now enforced, preventing forward-return label leakage.
        - F4: the fold boundaries now match the canonical utility, so a
          bug fix in ``make_folds`` propagates to both paths.

        The fold sizing (``min_train``, ``fold_size``) preserves the
        previous Path B layout when ``purge_bars == 0`` so existing
        artifact hashes remain comparable for datasets trained without a
        purge gap. When ``purge_bars > 0`` the ``fold_size`` is shrunk to
        make room for the purge budget so the folds still fit in
        ``n_rows`` (the canonical ``make_folds`` requires
        ``train_min + n_folds*(purge + val) <= n``).

        Returns a list of ``fincept_core.datasets.cv.Fold`` objects.
        """
        if _make_folds is None:  # pragma: no cover - fincept-core always present
            raise TrainingFailure(
                error_code="missing_dependency",
                error_summary="fincept_core.datasets.cv.make_folds not available",
            )
        min_train = max(10, n_rows // (n_folds + 2))
        # Shrink fold_size to make room for the purge budget so the folds
        # fit in n_rows. Without this, make_folds raises ValueError for
        # small datasets whenever purge_bars > 0.
        purge_budget = n_folds * purge_bars
        fold_size = max(5, (n_rows - min_train - purge_budget) // n_folds)
        return _make_folds(
            n_rows,
            n_folds=n_folds,
            train_min_bars=min_train,
            val_bars=fold_size,
            purge_bars=purge_bars,
            embargo_bars=0,
        )

    # --- Tier 2.3: triple-barrier / multiclass label helpers ------------

    def _is_multiclass(self) -> bool:
        """Check if the task is multiclass (e.g. triple-barrier labels)."""
        return self.task_spec is not None and self.task_spec.task_type == "multiclass"

    def _map_labels_for_lgb(self, y: Any) -> tuple[Any, dict[int, int]]:
        """Map labels to LightGBM-compatible integer classes.

        For multiclass (triple-barrier) tasks, labels like {-1, 0, +1}
        are mapped to {0, 1, 2} (LightGBM requires non-negative integer
        labels starting at 0). Returns the mapped labels and the
        label_map {original: mapped}.

        For binary/regression tasks, labels are returned unchanged.
        """
        import numpy as np

        if not self._is_multiclass():
            return y, {}
        unique_labels = sorted(set(int(v) for v in np.unique(y)))
        label_map = {orig: idx for idx, orig in enumerate(unique_labels)}
        mapped = np.array([label_map[int(v)] for v in y], dtype=y.dtype)
        return mapped, label_map

    def _compute_fold_accuracy(
        self,
        train_pred: Any,
        y_train: Any,
        val_pred: Any,
        y_val: Any,
    ) -> tuple[float, float]:
        """Compute train/val accuracy for the current fold.

        For binary tasks: ``(pred > 0.5) == (y > 0.5)`` (existing).
        For multiclass tasks: ``argmax(pred) == y`` (Tier 2.3).
        """
        import numpy as np

        if self._is_multiclass():
            train_pred_arr = np.asarray(train_pred)
            val_pred_arr = np.asarray(val_pred)
            # Multiclass: predictions are (n_samples, n_classes) probabilities.
            if train_pred_arr.ndim == 2:
                train_acc = float(np.mean(train_pred_arr.argmax(axis=1) == np.asarray(y_train)))
            else:
                train_acc = float(np.mean(train_pred_arr == np.asarray(y_train)))
            if val_pred_arr.ndim == 2:
                val_acc = float(np.mean(val_pred_arr.argmax(axis=1) == np.asarray(y_val)))
            else:
                val_acc = float(np.mean(val_pred_arr == np.asarray(y_val)))
            return train_acc, val_acc

        # Binary: existing threshold-at-0.5 pattern.
        train_acc = float(np.mean((np.asarray(train_pred) > 0.5) == (np.asarray(y_train) > 0.5)))
        val_acc = float(np.mean((np.asarray(val_pred) > 0.5) == (np.asarray(y_val) > 0.5)))
        return train_acc, val_acc

    # --- Tier 2.3b: meta-labeling helpers --------------------------------

    def _is_meta_labeling(self) -> bool:
        """Check if this is a meta-labeling run (Tier 2.3b).

        When True, the trainer trains a primary multiclass model on
        triple-barrier labels, then trains a secondary binary
        meta-model that decides whether to act on the primary signal.
        """
        return self.task_spec is not None and self.task_spec.meta_label_config is not None

    def _compute_meta_labels(
        self,
        oof_preds: Any,
        oof_labels: Any,
    ) -> tuple[Any, Any]:
        """Compute meta-labels from out-of-fold primary predictions.

        Takes the primary model's OOF predictions (n, n_classes)
        and the original triple-barrier labels (n,), converts the
        predictions to sides via argmax + reverse label-map, then
        computes meta-labels: 1 if side == label, 0 otherwise.

        Returns ``(meta_labels, sides)`` — meta_labels is a binary
        {0, 1} array, sides is the primary model's directional signal
        {-1, 0, +1} array (used as an additional feature for the
        meta-model).
        """
        import numpy as np

        from fincept_core.datasets.labels import meta_labels

        preds_arr = np.asarray(oof_preds, dtype=np.float64)
        labels_arr = np.asarray(oof_labels, dtype=np.float64)

        # Convert OOF predictions to predicted class indices.
        if preds_arr.ndim == 2:
            pred_classes = preds_arr.argmax(axis=1)
        else:
            pred_classes = preds_arr.astype(int)

        # Reverse-map predicted class indices to original label values.
        if self._label_map:
            inv_map = {v: k for k, v in self._label_map.items()}
            sides = np.array(
                [inv_map.get(int(c), 0) for c in pred_classes],
                dtype=np.float64,
            )
        else:
            sides = pred_classes.astype(np.float64)

        # Original labels are already in the original space (before
        # _map_labels_for_lgb was applied). But the OOF labels stored
        # in the metrics dict are the MAPPED labels (0, 1, 2). We need
        # to reverse-map them too for the meta_labels() comparison.
        if self._label_map:
            inv_map = {v: k for k, v in self._label_map.items()}
            original_labels = np.array(
                [inv_map.get(int(c), 0) for c in labels_arr.astype(int)],
                dtype=np.float64,
            )
        else:
            original_labels = labels_arr

        meta = np.array(
            meta_labels(
                sides.astype(int).tolist(),
                original_labels.astype(int).tolist(),
            ),
            dtype=np.float64,
        )
        return meta, sides

    def _train_meta_model(
        self,
        X: Any,
        y: Any,
        primary_model: Any,
        seed: int,
        req: RunPodTrainingRequest,
    ) -> tuple[Any, dict[str, Any]]:
        """Train the secondary (meta) binary classifier.

        The meta-model is a binary LightGBM classifier trained on
        ``(features + primary_side) → meta_label`` where meta_label
        is 1 if the primary signal was correct, 0 otherwise.

        The primary model's predictions on the full dataset provide
        the side signal. The meta-model's own walk-forward validation
        provides the out-of-sample estimate.

        Returns ``(meta_model, meta_metrics)``.
        """
        import lightgbm as lgb
        import numpy as np

        from fincept_core.datasets.labels import meta_labels

        # Get the primary model's predictions on the full dataset.
        primary_preds = primary_model.predict(X)
        preds_arr = np.asarray(primary_preds, dtype=np.float64)

        # Convert predictions to sides (directional signals).
        if preds_arr.ndim == 2:
            pred_classes = preds_arr.argmax(axis=1)
        else:
            pred_classes = preds_arr.astype(int)

        # Reverse-map predicted class indices to original label values.
        if self._label_map:
            inv_map = {v: k for k, v in self._label_map.items()}
            sides = np.array(
                [inv_map.get(int(c), 0) for c in pred_classes],
                dtype=np.float64,
            )
            # Also reverse-map the training labels to original space.
            original_labels = np.array(
                [inv_map.get(int(v), 0) for v in y.astype(int)],
                dtype=np.float64,
            )
        else:
            sides = pred_classes.astype(np.float64)
            original_labels = np.asarray(y, dtype=np.float64)

        # Compute meta-labels: 1 if side == label, 0 otherwise.
        meta_labels_arr = np.array(
            meta_labels(
                sides.astype(int).tolist(),
                original_labels.astype(int).tolist(),
            ),
            dtype=np.float64,
        )

        # Augment features with the primary model's side signal.
        X_meta = np.column_stack([X, sides.reshape(-1, 1)])

        # Build binary LightGBM params for the meta-model.
        # We can't reuse _build_lgb_params because it will produce
        # multiclass params (the task_spec is multiclass). Build from
        # scratch with the same deterministic settings.
        params: dict[str, Any] = {
            "objective": "binary",
            "metric": "binary_logloss",
            "verbosity": -1,
            "seed": seed,
            "deterministic": True,
            "num_threads": 1,
            "num_leaves": 31,
            "learning_rate": 0.1,
        }
        # Override from search_space (excluding multiclass-specific keys).
        for key in ("num_leaves", "learning_rate", "max_depth", "min_data_in_leaf"):
            if key in req.search_space:
                vals = req.search_space[key]
                if isinstance(vals, list) and vals:
                    params[key] = vals[0]
        n_estimators = self._get_n_estimators(req)

        # Simple k-fold validation for the meta-model (same fold
        # structure as the primary model — we use the same heuristic
        # walk-forward split).
        n = X_meta.shape[0]
        n_folds = min(self.n_folds, max(2, n // 50))
        fold_size = n // n_folds
        all_meta_preds: list[float] = []
        all_meta_labels: list[float] = []
        fold_accs: list[float] = []

        for i in range(n_folds):
            val_start = i * fold_size
            val_end = (i + 1) * fold_size if i < n_folds - 1 else n
            val_idx = np.arange(val_start, val_end)
            train_idx = np.concatenate(
                [
                    np.arange(0, val_start),
                    np.arange(val_end, n),
                ]
            )

            X_tr, X_va = X_meta[train_idx], X_meta[val_idx]
            y_tr, y_va = meta_labels_arr[train_idx], meta_labels_arr[val_idx]

            if len(np.unique(y_tr)) < 2:
                # Skip degenerate folds (all one class).
                continue

            train_set = lgb.Dataset(X_tr, label=y_tr)
            val_set = lgb.Dataset(X_va, label=y_va, reference=train_set)
            es_val = req.search_space.get("early_stopping_rounds")
            callbacks = []
            if es_val:
                callbacks.append(lgb.early_stopping(int(es_val[0]), verbose=False))

            model = lgb.train(
                params,
                train_set,
                num_boost_round=n_estimators,
                valid_sets=[val_set],
                callbacks=cast("list[Callable[..., Any]] | None", callbacks),
            )
            val_pred = cast("Any", model.predict(X_va))
            all_meta_preds.extend(val_pred.tolist())
            all_meta_labels.extend(cast("Any", y_va).tolist())
            fold_accs.append(float(np.mean((val_pred > 0.5) == (cast("Any", y_va) > 0.5))))

        # Compute meta-model metrics.
        preds_arr = np.array(all_meta_preds, dtype=np.float64)
        labels_arr = np.array(all_meta_labels, dtype=np.float64)
        meta_accuracy = float(np.mean((preds_arr > 0.5) == (labels_arr > 0.5)))
        eps = 1e-15
        pred_clipped = np.clip(preds_arr, eps, 1 - eps)
        meta_logloss = float(
            -np.mean(
                labels_arr * np.log(pred_clipped) + (1 - labels_arr) * np.log(1 - pred_clipped),
            )
        )
        meta_brier = float(np.mean((preds_arr - labels_arr) ** 2))

        # Train final meta-model on all data.
        final_meta_model = lgb.train(
            params,
            lgb.Dataset(X_meta, label=meta_labels_arr),
            num_boost_round=n_estimators,
        )

        meta_metrics = {
            "meta_accuracy": meta_accuracy,
            "meta_logloss": meta_logloss,
            "meta_brier_score": meta_brier,
            "meta_n_folds": len(fold_accs),
            "meta_avg_fold_accuracy": float(np.mean(fold_accs)) if fold_accs else 0.0,
            "meta_positive_rate": float(np.mean(meta_labels_arr)),
        }
        return final_meta_model, meta_metrics

    # --- Tier 2.7: checkpoint/resume helpers -----------------------------

    def _should_skip_fold(self, fold_position: int) -> bool:
        """Check if a fold should be skipped (already checkpointed).

        Returns True when ``resume_from_fold`` is set and the fold's
        0-based position is <= resume_from_fold (i.e. the fold was
        completed in a previous run and has a checkpoint).
        """
        return self.resume_from_fold is not None and fold_position <= self.resume_from_fold

    def _save_fold_checkpoint(
        self,
        fold_position: int,
        model: Any,
        train_acc: float,
        val_acc: float,
        total_folds: int,
    ) -> None:
        """Save a per-fold checkpoint (Tier 2.7).

        Pickles the LightGBM/XGBoost model and fold metrics so a
        preempted run can resume from the next fold. Best-effort: a
        checkpoint write failure is logged but does NOT fail the
        training job (the final artifact is what matters; checkpoints
        are only for resume convenience).
        """
        if self.checkpoint_manager is None:
            return
        if not self.checkpoint_manager.should_save(fold_position):
            return
        import pickle

        try:
            fold_model_bytes = pickle.dumps(model, protocol=pickle.HIGHEST_PROTOCOL)
            self.checkpoint_manager.save(
                fold_index=fold_position,
                fold_model=fold_model_bytes,
                fold_metrics={
                    "train_acc": float(train_acc),
                    "val_acc": float(val_acc),
                },
                total_folds=total_folds,
            )
        except Exception:
            # Best-effort: don't fail training because a checkpoint
            # write failed (e.g. disk full on the network volume).
            pass

    # --- walk-forward validation -----------------------------------------

    def _walk_forward_validate(
        self,
        X: Any,
        y: Any,
        timestamps: Any,
        seed: int,
        deadline_ns: int,
        req: RunPodTrainingRequest,
        *,
        weights: Any = None,
        groups: Any = None,
        fold_assignment: Any = None,
    ) -> dict[str, Any]:
        """Walk-forward validation with expanding window + purge gap.

        For each fold, trains on all data before the fold (minus a purge
        gap) and validates on the fold. Collects out-of-sample predictions
        for metric computation.

        The purge gap (``val_start - train_end >= purge_bars``) prevents
        forward-return label leakage: a training row at time ``t`` has a
        label that depends on prices at ``t + horizon_bars``, so without
        a gap the last ``horizon_bars`` training rows leak into validation.
        See docs/TRAINING_ANALYSIS.md finding F2.

        Fold boundaries are delegated to the canonical
        ``fincept_core.datasets.cv.make_folds`` (fixing F4 — Path B now
        matches Path A and the backtester).

        Phase 8 / T-8.1: when ``weights`` is provided, sample weights are
        passed to the LightGBM ``Dataset``. When ``groups`` is provided
        and the task is ranking, group boundaries are computed and passed
        to the ``Dataset``.

        T-8.4: when ``fold_assignment`` is not None, the manifest-declared
        fold windows are consumed exactly (via ``get_fold_data``) instead
        of re-deriving fold boundaries from the data.

        T-8.2: when ``groups`` is provided, the validation groups +
        timestamps are collected alongside predictions/labels so rank
        metrics can be computed after validation.
        """
        import lightgbm as lgb
        import numpy as np

        n = len(y)
        if n < 10:
            raise TrainingFailure(
                error_code="insufficient_data",
                error_summary=f"dataset too small for walk-forward validation: {n} rows",
            )

        params = self._build_lgb_params(seed, req)

        # Tier 2.3: map triple-barrier labels {-1,0,+1} → {0,1,2} for
        # multiclass LightGBM and set num_class.
        label_map: dict[int, int] = {}
        if self._is_multiclass():
            y, label_map = self._map_labels_for_lgb(y)
            params["num_class"] = int(max(label_map.values())) + 1 if label_map else 3
            self._label_map = label_map
        n_estimators = self._get_n_estimators(req)

        # Early stopping: if early_stopping_rounds is set in search_space,
        # use validation-based early stopping to prevent overfitting.
        early_stopping_rounds = None
        es_val = req.search_space.get("early_stopping_rounds")
        if es_val:
            early_stopping_rounds = int(es_val[0])

        all_preds: list[float] = []
        all_labels: list[float] = []
        all_groups: list[Any] = []
        all_timestamps: list[Any] = []
        fold_train_acc: list[float] = []
        fold_val_acc: list[float] = []
        fold_best_iterations: list[int] = []

        # --- T-8.4: manifest fold consumption path ----------------------
        if fold_assignment is not None:
            from quant_foundry.fold_consumer import get_fold_data

            fold_ids = sorted(fw.fold_id for fw in fold_assignment.fold_spec.folds)
            fold_position = 0
            total_folds = len(fold_ids)
            for fid in fold_ids:
                if time.time_ns() >= deadline_ns:
                    raise TrainingFailure(
                        error_code="timeout",
                        error_summary=f"training deadline breached during manifest fold {fid}",
                    )
                # Tier 2.7: skip already-checkpointed folds on resume.
                if self._should_skip_fold(fold_position):
                    fold_position += 1
                    continue
                train_idx, val_idx = get_fold_data(fold_assignment, fid)
                if not train_idx or not val_idx:
                    fold_position += 1
                    continue
                X_train = X[train_idx]
                y_train = y[train_idx]
                X_val = X[val_idx]
                y_val = y[val_idx]

                if len(np.unique(y_train)) < 2:
                    fold_position += 1
                    continue

                train_kwargs: dict[str, Any] = {}
                if weights is not None:
                    train_kwargs["weight"] = weights[train_idx]
                train_set = lgb.Dataset(X_train, label=y_train, **train_kwargs)

                if early_stopping_rounds and len(X_val) > 0:
                    val_set = lgb.Dataset(X_val, label=y_val, reference=train_set)
                    callbacks = [
                        lgb.early_stopping(
                            stopping_rounds=early_stopping_rounds,
                            verbose=False,
                        ),
                        lgb.log_evaluation(period=0),
                    ]
                    model = lgb.train(
                        params,
                        train_set,
                        num_boost_round=n_estimators,
                        valid_sets=[val_set],
                        callbacks=cast("list[Callable[..., Any]] | None", callbacks),
                    )
                    fold_best_iterations.append(model.best_iteration)
                else:
                    model = lgb.train(
                        params,
                        train_set,
                        num_boost_round=n_estimators,
                    )
                    fold_best_iterations.append(n_estimators)

                train_pred = np.asarray(model.predict(X_train), dtype=np.float64)
                val_pred = np.asarray(model.predict(X_val), dtype=np.float64)
                train_acc, val_acc = self._compute_fold_accuracy(
                    train_pred, y_train, val_pred, y_val
                )

                fold_train_acc.append(train_acc)
                fold_val_acc.append(val_acc)
                all_preds.extend(val_pred.tolist())
                all_labels.extend(y_val.tolist())
                if groups is not None:
                    all_groups.extend(groups[val_idx].tolist())
                if timestamps is not None:
                    all_timestamps.extend(timestamps[val_idx].tolist())
                # Tier 2.7: save per-fold checkpoint.
                self._save_fold_checkpoint(fold_position, model, train_acc, val_acc, total_folds)
                fold_position += 1
        else:
            # --- heuristic walk-forward fold path (existing) ------------
            order = np.argsort(timestamps, kind="stable")
            X_s = X[order]
            y_s = y[order]
            weights_s = weights[order] if weights is not None else None
            groups_s = groups[order] if groups is not None else None
            timestamps_s = timestamps[order] if timestamps is not None else None

            horizon_bars = self._resolve_horizon_bars(req.extra_constraints)
            purge_bars = self._resolve_purge_bars(horizon_bars, req.extra_constraints)

            try:
                folds = self._build_walk_forward_folds(
                    n_rows=n,
                    purge_bars=purge_bars,
                    n_folds=self.n_folds,
                )
            except ValueError as exc:
                raise TrainingFailure(
                    error_code="insufficient_data",
                    error_summary=str(exc),
                ) from exc

            for fold in folds:
                if time.time_ns() >= deadline_ns:
                    raise TrainingFailure(
                        error_code="timeout",
                        error_summary=f"training deadline breached during fold {fold.index}",
                    )

                # Tier 2.7: skip already-checkpointed folds on resume.
                if self._should_skip_fold(fold.index):
                    continue

                train_end = fold.train_end
                val_start = fold.val_start
                val_end = fold.val_end

                if val_start >= n or val_end <= val_start:
                    break

                X_train = X_s[:train_end]
                y_train = y_s[:train_end]
                X_val = X_s[val_start:val_end]
                y_val = y_s[val_start:val_end]

                if len(np.unique(y_train)) < 2:
                    continue

                train_kwargs: dict[str, Any] = {}  # type: ignore[no-redef]  # redefined in walk-forward branch below
                if weights_s is not None:
                    w_train = weights_s[:train_end]
                    train_kwargs["weight"] = w_train
                train_set = lgb.Dataset(X_train, label=y_train, **train_kwargs)

                if early_stopping_rounds and len(X_val) > 0:
                    val_set = lgb.Dataset(X_val, label=y_val, reference=train_set)
                    callbacks = [
                        lgb.early_stopping(
                            stopping_rounds=early_stopping_rounds,
                            verbose=False,
                        ),
                        lgb.log_evaluation(period=0),
                    ]
                    model = lgb.train(
                        params,
                        train_set,
                        num_boost_round=n_estimators,
                        valid_sets=[val_set],
                        callbacks=cast("list[Callable[..., Any]] | None", callbacks),
                    )
                    fold_best_iterations.append(model.best_iteration)
                else:
                    model = lgb.train(
                        params,
                        train_set,
                        num_boost_round=n_estimators,
                    )
                    fold_best_iterations.append(n_estimators)

                train_pred = np.asarray(model.predict(X_train), dtype=np.float64)
                val_pred = np.asarray(model.predict(X_val), dtype=np.float64)
                train_acc, val_acc = self._compute_fold_accuracy(
                    train_pred, y_train, val_pred, y_val
                )

                fold_train_acc.append(train_acc)
                fold_val_acc.append(val_acc)
                all_preds.extend(val_pred.tolist())
                all_labels.extend(y_val.tolist())
                if groups_s is not None:
                    all_groups.extend(groups_s[val_start:val_end].tolist())
                if timestamps_s is not None:
                    all_timestamps.extend(timestamps_s[val_start:val_end].tolist())
                # Tier 2.7: save per-fold checkpoint.
                self._save_fold_checkpoint(fold.index, model, train_acc, val_acc, len(folds))

        if not all_preds:
            raise TrainingFailure(
                error_code="no_validation_data",
                error_summary=(
                    "no validation folds produced predictions (dataset too small or single-class)"
                ),
            )

        preds_arr = np.array(all_preds, dtype=np.float64)
        labels_arr = np.array(all_labels, dtype=np.float64)

        periods_per_year = self._resolve_periods_per_year(req.extra_constraints)

        result = self._compute_metrics(
            preds_arr,
            labels_arr,
            fold_train_acc,
            fold_val_acc,
            periods_per_year=periods_per_year,
        )
        result["fold_best_iterations"] = fold_best_iterations
        result["avg_best_iteration"] = (
            sum(fold_best_iterations) / len(fold_best_iterations)
            if fold_best_iterations
            else n_estimators
        )
        # T-8.2: stash the out-of-sample predictions + groups +
        # timestamps so rank metrics can be computed after validation.
        result["_oos_preds"] = preds_arr
        result["_oos_labels"] = labels_arr
        result["_oos_groups"] = np.array(all_groups) if all_groups else None
        result["_oos_timestamps"] = np.array(all_timestamps) if all_timestamps else None
        # Store for final model training (use avg best iteration)
        self._last_avg_best_iteration = result["avg_best_iteration"]
        return result

    # --- CPCV validation (Tier 2.1) ---------------------------------------

    def _cpcv_validate(
        self,
        X: Any,
        y: Any,
        timestamps: Any,
        seed: int,
        deadline_ns: int,
        req: RunPodTrainingRequest,
        *,
        weights: Any = None,
        groups: Any = None,
    ) -> dict[str, Any]:
        """Combinatorial Purged Cross-Validation (CPCV).

        Splits the sorted data into ``cpcv_n_groups`` contiguous blocks
        and for every combination of ``cpcv_n_val_groups`` blocks as
        validation, trains on the remaining blocks (minus purged bars at
        validation boundaries) and validates on the held-out blocks.

        Unlike expanding-window walk-forward, CPCV training data is
        non-contiguous (a union of blocks). This produces C(N, P) folds
        and enables the real CSCV PBO estimator (Bailey, Borwein,
        López de Prado, Zhu 2017) — each fold's IS/OOS return series
        becomes a "candidate" for the PBO computation.

        See ``fincept_core.datasets.cv.make_cpcv_folds`` for the fold
        generation logic and ``quant_foundry.pbo.probability_of_backtest_overfitting``
        for the PBO estimator.
        """
        import lightgbm as lgb
        import numpy as np

        n = len(y)
        if n < 10:
            raise TrainingFailure(
                error_code="insufficient_data",
                error_summary=f"dataset too small for CPCV validation: {n} rows",
            )

        try:
            from fincept_core.datasets import make_cpcv_folds as _make_cpcv_folds
        except ImportError:
            raise TrainingFailure(
                error_code="missing_dependency",
                error_summary="fincept_core.datasets.cv.make_cpcv_folds not available",
            )

        params = self._build_lgb_params(seed, req)
        n_estimators = self._get_n_estimators(req)

        # Tier 2.3: map triple-barrier labels {-1,0,+1} → {0,1,2} for
        # multiclass LightGBM and set num_class.
        if self._is_multiclass():
            y, _label_map = self._map_labels_for_lgb(y)
            params["num_class"] = int(max(_label_map.values())) + 1 if _label_map else 3

        early_stopping_rounds = None
        es_val = req.search_space.get("early_stopping_rounds")
        if es_val:
            early_stopping_rounds = int(es_val[0])

        # Sort by timestamp (CPCV requires temporal ordering within blocks)
        order = np.argsort(timestamps, kind="stable")
        X_s = X[order]
        y_s = y[order]
        weights_s = weights[order] if weights is not None else None
        groups_s = groups[order] if groups is not None else None
        timestamps_s = timestamps[order] if timestamps is not None else None

        horizon_bars = self._resolve_horizon_bars(req.extra_constraints)
        purge_bars = self._resolve_purge_bars(horizon_bars, req.extra_constraints)

        try:
            cpcv_folds = _make_cpcv_folds(
                n,
                n_groups=self.cpcv_n_groups,
                n_val_groups=self.cpcv_n_val_groups,
                purge_bars=purge_bars,
            )
        except ValueError as exc:
            raise TrainingFailure(
                error_code="insufficient_data",
                error_summary=str(exc),
            ) from exc

        all_preds: list[float] = []
        all_labels: list[float] = []
        all_groups: list[Any] = []
        all_timestamps: list[Any] = []
        fold_train_acc: list[float] = []
        fold_val_acc: list[float] = []
        fold_best_iterations: list[int] = []
        # Per-fold IS/OOS return series for CSCV PBO computation.
        # Each fold is treated as a "candidate" — the CSCV method
        # concatenates IS+OOS returns and checks if the IS-optimal
        # fold underperforms the median OOS rank across combinations.
        fold_is_returns: list[list[float]] = []
        fold_oos_returns: list[list[float]] = []

        for fold in cpcv_folds:
            if time.time_ns() >= deadline_ns:
                raise TrainingFailure(
                    error_code="timeout",
                    error_summary=f"training deadline breached during CPCV fold {fold.index}",
                )

            # Tier 2.7: skip already-checkpointed folds on resume.
            if self._should_skip_fold(fold.index):
                continue

            # Build non-contiguous training index from train_ranges
            train_idx_list: list[int] = []
            for s, e in fold.train_ranges:
                train_idx_list.extend(range(s, e))
            val_idx_list: list[int] = []
            for s, e in fold.val_ranges:
                val_idx_list.extend(range(s, e))

            if not train_idx_list or not val_idx_list:
                continue

            train_idx = np.array(train_idx_list)
            val_idx = np.array(val_idx_list)

            X_train = X_s[train_idx]
            y_train = y_s[train_idx]
            X_val = X_s[val_idx]
            y_val = y_s[val_idx]

            if len(np.unique(y_train)) < 2:
                continue

            train_kwargs: dict[str, Any] = {}
            if weights_s is not None:
                train_kwargs["weight"] = weights_s[train_idx]
            train_set = lgb.Dataset(X_train, label=y_train, **train_kwargs)

            if early_stopping_rounds and len(X_val) > 0:
                val_set = lgb.Dataset(X_val, label=y_val, reference=train_set)
                callbacks = [
                    lgb.early_stopping(
                        stopping_rounds=early_stopping_rounds,
                        verbose=False,
                    ),
                    lgb.log_evaluation(period=0),
                ]
                model = lgb.train(
                    params,
                    train_set,
                    num_boost_round=n_estimators,
                    valid_sets=[val_set],
                    callbacks=cast("list[Callable[..., Any]] | None", callbacks),
                )
                fold_best_iterations.append(model.best_iteration)
            else:
                model = lgb.train(
                    params,
                    train_set,
                    num_boost_round=n_estimators,
                )
                fold_best_iterations.append(n_estimators)

            train_pred = np.asarray(model.predict(X_train), dtype=np.float64)
            val_pred = np.asarray(model.predict(X_val), dtype=np.float64)
            train_acc, val_acc = self._compute_fold_accuracy(train_pred, y_train, val_pred, y_val)

            fold_train_acc.append(train_acc)
            fold_val_acc.append(val_acc)
            all_preds.extend(val_pred.tolist())
            all_labels.extend(y_val.tolist())
            if groups_s is not None:
                all_groups.extend(groups_s[val_idx].tolist())
            if timestamps_s is not None:
                all_timestamps.extend(timestamps_s[val_idx].tolist())

            # Collect per-fold IS/OOS returns for CSCV PBO.
            # Returns = positions * (2*labels - 1), where positions = 2*pred - 1.
            is_positions = 2 * train_pred - 1
            is_returns = (is_positions * (2 * y_train - 1)).tolist()
            oos_positions = 2 * val_pred - 1
            oos_returns = (oos_positions * (2 * y_val - 1)).tolist()
            fold_is_returns.append([float(r) for r in is_returns])
            fold_oos_returns.append([float(r) for r in oos_returns])

            # Tier 2.7: save per-fold checkpoint.
            self._save_fold_checkpoint(fold.index, model, train_acc, val_acc, len(cpcv_folds))

        if not all_preds:
            raise TrainingFailure(
                error_code="no_validation_data",
                error_summary=(
                    "no CPCV folds produced predictions (dataset too small or single-class)"
                ),
            )

        preds_arr = np.array(all_preds, dtype=np.float64)
        labels_arr = np.array(all_labels, dtype=np.float64)

        periods_per_year = self._resolve_periods_per_year(req.extra_constraints)

        result = self._compute_metrics(
            preds_arr,
            labels_arr,
            fold_train_acc,
            fold_val_acc,
            periods_per_year=periods_per_year,
            fold_is_returns=fold_is_returns,
            fold_oos_returns=fold_oos_returns,
        )
        result["fold_best_iterations"] = fold_best_iterations
        result["avg_best_iteration"] = (
            sum(fold_best_iterations) / len(fold_best_iterations)
            if fold_best_iterations
            else n_estimators
        )
        result["_oos_preds"] = preds_arr
        result["_oos_labels"] = labels_arr
        result["_oos_groups"] = np.array(all_groups) if all_groups else None
        result["_oos_timestamps"] = np.array(all_timestamps) if all_timestamps else None
        result["cv_mode"] = "cpcv"
        result["cpcv_n_groups"] = self.cpcv_n_groups
        result["cpcv_n_val_groups"] = self.cpcv_n_val_groups
        result["cpcv_n_folds"] = len(cpcv_folds)
        self._last_avg_best_iteration = result["avg_best_iteration"]
        return result

    # --- metric computation ----------------------------------------------

    def _compute_metrics(
        self,
        all_preds: Any,
        all_labels: Any,
        fold_train_acc: list[float],
        fold_val_acc: list[float],
        *,
        periods_per_year: int | None = None,
        fold_is_returns: list[list[float]] | None = None,
        fold_oos_returns: list[list[float]] | None = None,
    ) -> dict[str, Any]:
        """Compute real evaluation metrics from out-of-sample predictions.

        Args:
            periods_per_year: annualization factor for the Sharpe ratio
                (``sqrt(periods_per_year)``). When ``None``, falls back to
                the trainer's ``annualization_factor`` field (backward
                compat). When provided, the Sharpe is annualized correctly
                for the bar frequency — e.g. ``525_600`` for 1-minute
                crypto bars (24/7), not ``252`` (daily equities). See
                docs/TRAINING_ANALYSIS.md finding F1.

        PBO / deflated_sharpe note (F3):
            The ``pbo`` field is a **fold-level overfit ratio** — the
            fraction of folds where val accuracy was below train accuracy.
            It is NOT the academic Bailey & Lopez de Prado Probability of
            Backtest Overfitting (which requires combinatorially purged
            cross-validation across multiple strategy configurations).
            The schema field name ``pbo`` is kept for backward compat
            with the tournament/leaderboard/promotion pipeline; the
            method is recorded in the returned dict as ``pbo_method`` so
            an operator inspecting the dossier knows what was computed.
            The tournament's own DSR
            (``quant_foundry.significance.deflated_sharpe_ratio``) is the
            real Bailey & Lopez de Prado DSR and is NOT affected by this
            crude heuristic.
        """
        import numpy as np

        # Tier 2.3: multiclass (triple-barrier) metrics branch.
        # Predictions are (n, n_classes) probabilities; labels are integer
        # class indices. We compute argmax-based accuracy, multiclass
        # logloss, and map the predicted class back to a position for
        # Sharpe/returns computation.
        if self._is_multiclass() and np.asarray(all_preds).ndim == 2:
            return self._compute_multiclass_metrics(
                all_preds,
                all_labels,
                fold_train_acc,
                fold_val_acc,
                periods_per_year=periods_per_year,
                fold_is_returns=fold_is_returns,
                fold_oos_returns=fold_oos_returns,
            )

        pred_binary = (all_preds > 0.5).astype(np.float64)
        accuracy = float(np.mean(pred_binary == all_labels))

        eps = 1e-15
        pred_clipped = np.clip(all_preds, eps, 1 - eps)
        logloss = float(
            -np.mean(
                all_labels * np.log(pred_clipped) + (1 - all_labels) * np.log(1 - pred_clipped),
            ),
        )

        brier = float(np.mean((all_preds - all_labels) ** 2))

        n_buckets = 10
        bucket_probs: list[float] = []
        bucket_actuals: list[float] = []
        for i in range(n_buckets):
            lo = i / n_buckets
            hi = (i + 1) / n_buckets
            if i < n_buckets - 1:
                mask = (all_preds >= lo) & (all_preds < hi)
            else:
                mask = (all_preds >= lo) & (all_preds <= hi)
            if np.any(mask):
                bucket_probs.append(float(np.mean(all_preds[mask])))
                bucket_actuals.append(float(np.mean(all_labels[mask])))

        positions = 2 * all_preds - 1
        returns = positions * (2 * all_labels - 1)
        win_rate = float(np.mean(returns > 0))

        # F1 fix: annualize using the bar-frequency-derived factor, not
        # the hardcoded 252 (daily). For 1-minute crypto bars this is
        # sqrt(525_600) ~ 725 instead of sqrt(252) ~ 15.9.
        ann_factor = periods_per_year if periods_per_year is not None else self.annualization_factor
        std_returns = float(np.std(returns))
        if std_returns > 0:
            sharpe = float(
                np.mean(returns) / std_returns * np.sqrt(ann_factor),
            )
        else:
            sharpe = 0.0

        cumulative = np.cumsum(returns)
        running_max = np.maximum.accumulate(cumulative)
        drawdowns = cumulative - running_max
        max_drawdown = float(np.min(drawdowns)) if len(drawdowns) > 0 else 0.0

        # Tier 2.5: execution-aware backtesting — compute cost-aware
        # (net-of-cost) training metrics alongside the frictionless
        # (gross) metrics. The Sharpe-769 artifact demonstrated that
        # frictionless metrics must never reach a promotion decision.
        # The default cost model matches the settlement default (5 bps
        # fee, 3 bps spread, 0 bps slippage) so training and settlement
        # share the same baseline cost assumptions.
        from quant_foundry.execution_costs import (
            DEFAULT_TRAINING_COST_MODEL,
            compute_cost_aware_metrics,
        )

        gross_returns_list = [float(r) for r in returns]
        positions_list = [float(p) for p in positions]
        cost_metrics = compute_cost_aware_metrics(
            gross_returns_list,
            positions_list,
            DEFAULT_TRAINING_COST_MODEL,
            ann_factor=float(np.sqrt(ann_factor)),
        )

        # F3: this is a fold-level overfit ratio, NOT the academic PBO.
        # The schema field name ``pbo`` is kept for backward compat; the
        # method is recorded below as ``pbo_method``.
        if fold_train_acc and fold_val_acc:
            overfit_count = sum(
                1 for t, v in zip(fold_train_acc, fold_val_acc, strict=False) if v < t
            )
            fold_overfit_ratio = float(overfit_count / len(fold_train_acc))
        else:
            fold_overfit_ratio = 0.5

        # Tier 2.1: when per-fold IS/OOS return series are available
        # (CPCV mode), compute the real CSCV PBO (Bailey, Borwein,
        # López de Prado, Zhu 2017) from ``pbo.py``. Each fold is
        # treated as a "candidate" — the CSCV method concatenates
        # IS+OOS returns, partitions into blocks, and checks if the
        # IS-optimal fold underperforms the median OOS rank across
        # combinatorial splits. This is the academic PBO, not the
        # fold_overfit_ratio placeholder.
        pbo_value = fold_overfit_ratio
        pbo_method = "fold_overfit_ratio"
        pbo_logit: float | None = None
        pbo_n_combinations: int | None = None
        pbo_flagged: bool | None = None
        if (
            fold_is_returns is not None
            and fold_oos_returns is not None
            and len(fold_is_returns) >= 2
        ):
            try:
                from quant_foundry.pbo import probability_of_backtest_overfitting as _pbo

                pbo_result = _pbo(
                    fold_is_returns,
                    fold_oos_returns,
                    n_partitions=min(16, len(fold_is_returns)),
                    seed=0,
                    threshold=0.1,
                )
                pbo_value = float(pbo_result.pbo)
                pbo_method = "cscv_cpcv"
                pbo_logit = float(pbo_result.logit)
                pbo_n_combinations = pbo_result.n_combinations
                pbo_flagged = pbo_result.flagged
            except Exception:
                # If CSCV PBO fails (e.g. degenerate returns), fall back
                # to the fold_overfit_ratio and record the method.
                pass

        # Tier 2.2: use the real Bailey & López de Prado Deflated Sharpe
        # Ratio from significance.py instead of the placeholder
        # ``sharpe * (1 - fold_overfit_ratio)``. The real DSR applies a
        # multiple-trials penalty (sqrt(2*ln(N))/sqrt(n)) and a
        # non-normality penalty (skew/kurtosis adjustment). The trial
        # count ``self.trial_count`` is the real number of Optuna trials
        # evaluated (Tier 1.4), passed from the handler. When no Optuna
        # search ran, ``trial_count`` defaults to 1 (single-trial DSR).
        # The DSR is computed on per-period returns, then annualized to
        # match the raw Sharpe's annualization.
        from quant_foundry.significance import deflated_sharpe_ratio as _dsr

        oos_returns_list = [float(r) for r in returns]
        dsr_result = _dsr(oos_returns_list, trial_count=self.trial_count)
        deflated_sharpe = float(dsr_result.deflated_sharpe * np.sqrt(ann_factor))

        return {
            "training_metrics": {
                "accuracy": accuracy,
                "logloss": logloss,
                "brier_score": brier,
                "sharpe_ratio": sharpe,
                "max_drawdown": max_drawdown,
                "win_rate": win_rate,
            },
            # Tier 2.5: execution-aware (net-of-cost) training metrics.
            # The gross metrics above are kept for the audit trail; the
            # net metrics below are what the promotion gate should use.
            "backtest_metrics": {
                "sharpe_gross": cost_metrics.sharpe_gross,
                "sharpe_net": cost_metrics.sharpe_net,
                "max_drawdown_gross": cost_metrics.max_drawdown_gross,
                "max_drawdown_net": cost_metrics.max_drawdown_net,
                "win_rate_gross": cost_metrics.win_rate_gross,
                "win_rate_net": cost_metrics.win_rate_net,
                "mean_return_gross": cost_metrics.mean_return_gross,
                "mean_return_net": cost_metrics.mean_return_net,
                "turnover": cost_metrics.turnover,
                "total_cost_bps": cost_metrics.total_cost_bps,
                "cost_model_version": cost_metrics.cost_model_version,
            },
            "sharpe_net": cost_metrics.sharpe_net,
            "max_drawdown_net": cost_metrics.max_drawdown_net,
            "win_rate_net": cost_metrics.win_rate_net,
            "turnover": cost_metrics.turnover,
            "total_cost_bps": cost_metrics.total_cost_bps,
            "cost_model_version": cost_metrics.cost_model_version,
            "pbo": pbo_value,
            "deflated_sharpe": deflated_sharpe,
            "pbo_method": pbo_method,
            "deflated_sharpe_method": "bailey_lopez_de_prado_dsr",
            "deflated_sharpe_raw": float(dsr_result.raw_sharpe * np.sqrt(ann_factor)),
            "deflated_sharpe_trial_count": dsr_result.trial_count,
            "deflated_sharpe_skew": dsr_result.skew,
            "deflated_sharpe_kurtosis": dsr_result.kurtosis,
            "deflated_sharpe_multiple_trials_penalty": float(
                dsr_result.multiple_trials_penalty * np.sqrt(ann_factor)
            ),
            "deflated_sharpe_non_normality_penalty": dsr_result.non_normality_penalty,
            "brier_score": brier,
            "win_rate": win_rate,
            "max_drawdown": max_drawdown,
            "sharpe_ratio": sharpe,
            "calibration_bucket_probs": bucket_probs,
            "calibration_bucket_actuals": bucket_actuals,
            "pbo_logit": pbo_logit,
            "pbo_n_combinations": pbo_n_combinations,
            "pbo_flagged": pbo_flagged,
        }

    def _compute_multiclass_metrics(
        self,
        all_preds: Any,
        all_labels: Any,
        fold_train_acc: list[float],
        fold_val_acc: list[float],
        *,
        periods_per_year: int | None = None,
        fold_is_returns: list[list[float]] | None = None,
        fold_oos_returns: list[list[float]] | None = None,
    ) -> dict[str, Any]:
        """Compute metrics for multiclass (triple-barrier) tasks (Tier 2.3).

        Predictions are (n, n_classes) softmax probabilities. Labels are
        integer class indices (mapped from original {-1, 0, +1}).

        Accuracy is argmax-based. Logloss is multiclass cross-entropy.
        Returns/Sharpe are computed by reverse-mapping the predicted class
        to a position: class 0 → -1 (short), class 1 → 0 (flat), class 2
        → +1 (long), using the stored ``_label_map``.
        """
        import numpy as np

        preds_arr = np.asarray(all_preds, dtype=np.float64)
        labels_arr = np.asarray(all_labels, dtype=np.float64)
        n_samples, n_classes = preds_arr.shape

        # argmax accuracy.
        pred_classes = preds_arr.argmax(axis=1)
        accuracy = float(np.mean(pred_classes == labels_arr))

        # Multiclass logloss (cross-entropy).
        eps = 1e-15
        pred_clipped = np.clip(preds_arr, eps, 1.0)
        # One-hot encode labels.
        one_hot = np.zeros((n_samples, n_classes))
        one_hot[np.arange(n_samples), labels_arr.astype(int)] = 1.0
        logloss = float(-np.mean(np.sum(one_hot * np.log(pred_clipped), axis=1)))

        # Brier score: sum of squared errors across class probabilities.
        brier = float(np.mean(np.sum((preds_arr - one_hot) ** 2, axis=1)))

        # Reverse-map predicted class to original label value for
        # position/returns computation. The _label_map is
        # {original: mapped}, so we invert it to {mapped: original}.
        if self._label_map:
            inv_map = {v: k for k, v in self._label_map.items()}
            pred_positions = np.array(
                [float(inv_map.get(int(c), 0.0)) for c in pred_classes],
                dtype=np.float64,
            )
            label_positions = np.array(
                [float(inv_map.get(int(c), 0.0)) for c in labels_arr.astype(int)],
                dtype=np.float64,
            )
        else:
            # Fallback: assume class 0 = -1, 1 = 0, 2 = +1.
            class_to_pos = {0: -1.0, 1: 0.0, 2: 1.0}
            pred_positions = np.array(
                [class_to_pos.get(int(c), 0.0) for c in pred_classes],
                dtype=np.float64,
            )
            label_positions = np.array(
                [class_to_pos.get(int(c), 0.0) for c in labels_arr.astype(int)],
                dtype=np.float64,
            )

        # Returns: position * label_return (where label encodes direction).
        # For triple-barrier, the label IS the return direction.
        returns = pred_positions * label_positions
        win_rate = float(np.mean(returns > 0))

        # Sharpe ratio (annualized).
        ann_factor = periods_per_year if periods_per_year is not None else self.annualization_factor
        if len(returns) > 1 and np.std(returns) > 1e-12:
            sharpe = float(np.mean(returns) / np.std(returns) * np.sqrt(ann_factor))
        else:
            sharpe = 0.0

        # Max drawdown from cumulative returns.
        cum = np.cumsum(returns)
        running_max = np.maximum.accumulate(cum)
        drawdowns = cum - running_max
        max_drawdown = float(np.min(drawdowns)) if len(drawdowns) > 0 else 0.0

        # Fold overfit ratio.
        if fold_train_acc and fold_val_acc:
            overfit_count = sum(
                1 for t, v in zip(fold_train_acc, fold_val_acc, strict=False) if v < t
            )
            fold_overfit_ratio = float(overfit_count / len(fold_train_acc))
        else:
            fold_overfit_ratio = 0.5

        pbo_value = fold_overfit_ratio
        pbo_method = "fold_overfit_ratio"

        # DSR (same as binary path).
        from quant_foundry.significance import deflated_sharpe_ratio as _dsr

        oos_returns_list = [float(r) for r in returns]
        dsr_result = _dsr(oos_returns_list, trial_count=self.trial_count)
        deflated_sharpe = float(dsr_result.deflated_sharpe * np.sqrt(ann_factor))

        # Cost metrics (net-of-cost) — reuse the binary path's cost model.
        # For multiclass, positions are {-1, 0, +1} so turnover is
        # computed from position changes.
        try:
            from quant_foundry.execution_costs import (
                DEFAULT_TRAINING_COST_MODEL,
                compute_cost_aware_metrics,
            )

            gross_returns_list = [float(r) for r in returns]
            positions_list = [float(p) for p in pred_positions]
            cost_metrics = compute_cost_aware_metrics(
                gross_returns_list,
                positions_list,
                DEFAULT_TRAINING_COST_MODEL,
                ann_factor=float(np.sqrt(ann_factor)),
            )
        except Exception:
            # Fallback: if cost model fails, use gross as net.
            from quant_foundry.execution_costs import CostAwareMetrics

            cost_metrics = CostAwareMetrics(
                sharpe_gross=sharpe,
                sharpe_net=sharpe,
                max_drawdown_gross=max_drawdown,
                max_drawdown_net=max_drawdown,
                win_rate_gross=win_rate,
                win_rate_net=win_rate,
                mean_return_gross=float(np.mean(returns)),
                mean_return_net=float(np.mean(returns)),
                turnover=0.0,
                total_cost_bps=0.0,
                cost_model_version="fallback",
            )

        return {
            "training_metrics": {
                "accuracy": accuracy,
                "logloss": logloss,
                "brier_score": brier,
                "sharpe_ratio": sharpe,
                "max_drawdown": max_drawdown,
                "win_rate": win_rate,
            },
            "backtest_metrics": {
                "sharpe_gross": cost_metrics.sharpe_gross,
                "sharpe_net": cost_metrics.sharpe_net,
                "max_drawdown_gross": cost_metrics.max_drawdown_gross,
                "max_drawdown_net": cost_metrics.max_drawdown_net,
                "win_rate_gross": cost_metrics.win_rate_gross,
                "win_rate_net": cost_metrics.win_rate_net,
                "mean_return_gross": cost_metrics.mean_return_gross,
                "mean_return_net": cost_metrics.mean_return_net,
                "turnover": cost_metrics.turnover,
                "total_cost_bps": cost_metrics.total_cost_bps,
                "cost_model_version": cost_metrics.cost_model_version,
            },
            "sharpe_net": cost_metrics.sharpe_net,
            "max_drawdown_net": cost_metrics.max_drawdown_net,
            "win_rate_net": cost_metrics.win_rate_net,
            "turnover": cost_metrics.turnover,
            "total_cost_bps": cost_metrics.total_cost_bps,
            "cost_model_version": cost_metrics.cost_model_version,
            "pbo": pbo_value,
            "deflated_sharpe": deflated_sharpe,
            "pbo_method": pbo_method,
            "deflated_sharpe_method": "bailey_lopez_de_prado_dsr",
            "deflated_sharpe_raw": float(dsr_result.raw_sharpe * np.sqrt(ann_factor)),
            "deflated_sharpe_trial_count": dsr_result.trial_count,
            "deflated_sharpe_skew": dsr_result.skew,
            "deflated_sharpe_kurtosis": dsr_result.kurtosis,
            "deflated_sharpe_multiple_trials_penalty": float(
                dsr_result.multiple_trials_penalty * np.sqrt(ann_factor)
            ),
            "deflated_sharpe_non_normality_penalty": dsr_result.non_normality_penalty,
            "brier_score": brier,
            "win_rate": win_rate,
            "max_drawdown": max_drawdown,
            "sharpe_ratio": sharpe,
            "calibration_bucket_probs": [],
            "calibration_bucket_actuals": [],
            "pbo_logit": None,
            "pbo_n_combinations": None,
            "pbo_flagged": None,
        }

    # --- final model training --------------------------------------------

    def _train_final_model(
        self,
        X: Any,
        y: Any,
        seed: int,
        req: RunPodTrainingRequest,
        *,
        weights: Any = None,
    ) -> Any:
        """Train the final LightGBM model on all available data.

        If early stopping was used during walk-forward validation, use the
        average best iteration across folds as the number of boosting rounds
        for the final model. This prevents the final model from overfitting
        by training too many rounds on the full dataset.

        Phase 8 / T-8.1: when ``weights`` is provided, sample weights are
        passed to the LightGBM ``Dataset``.
        """
        import lightgbm as lgb

        params = self._build_lgb_params(seed, req)
        n_estimators = self._get_n_estimators(req)

        # Tier 2.3: map labels + set num_class for multiclass.
        if self._is_multiclass():
            y, _lm = self._map_labels_for_lgb(y)
            params["num_class"] = int(max(_lm.values())) + 1 if _lm else 3

        # Use avg_best_iteration from walk-forward if early stopping was used
        es_val = req.search_space.get("early_stopping_rounds")
        if es_val and hasattr(self, "_last_avg_best_iteration"):
            avg_iter = self._last_avg_best_iteration
            if avg_iter and avg_iter > 10:
                n_estimators = int(avg_iter)

        train_kwargs: dict[str, Any] = {}
        if weights is not None:
            train_kwargs["weight"] = weights
        train_set = lgb.Dataset(X, label=y, **train_kwargs)
        model = lgb.train(
            params,
            train_set,
            num_boost_round=n_estimators,
        )
        return model

    # --- T-8.4: production fail-closed ----------------------------------

    def _require_fold_spec_for_production(self) -> None:
        """Fail-closed guard: production manifests must declare a FoldSpec.

        Delegates to
        :func:`quant_foundry.fold_consumer.require_fold_spec_for_production`.
        When ``is_production`` is True and ``fold_spec`` is None, raises
        ``TrainingFailure`` (translated from the ``ValueError`` raised by
        the guard).
        """
        from quant_foundry.fold_consumer import (
            require_fold_spec_for_production,
        )

        try:
            require_fold_spec_for_production(
                self.fold_spec,
                is_production=self.is_production,
            )
        except ValueError as exc:
            raise TrainingFailure(
                error_code="missing_fold_spec",
                error_summary=str(exc),
            ) from exc

    # --- T-7.2 / T-7.3: multi-backend dispatch --------------------------

    def _train_with_backend(
        self,
        req: RunPodTrainingRequest,
        *,
        deadline_ns: int,
    ) -> tuple[ArtifactManifest, ModelDossier]:
        """Dispatch to the catboost or xgboost backend.

        Both backends REQUIRE ``column_roles`` + ``task_spec`` (fail-closed
        when None) because they have no legacy infer-by-dropping-names
        fallback.
        """
        if self.column_roles is None or self.task_spec is None:
            raise TrainingFailure(
                error_code="missing_column_roles",
                error_summary=(
                    f"backend {self.backend!r} requires column_roles + "
                    "task_spec (no legacy infer-by-dropping-names fallback)"
                ),
            )

        if time.time_ns() >= deadline_ns:
            raise TrainingFailure(
                error_code="timeout",
                error_summary="training deadline breached before backend dispatch",
            )

        if self.backend in ("catboost", "catboost_gpu"):
            # Tier 1.3: ``catboost_gpu`` reuses the CatBoostTrainer code path;
            # the task_type (GPU vs CPU) is selected inside
            # ``_build_catboost_params`` based on ``req.model_family``.
            return self._train_catboost(req, deadline_ns=deadline_ns)
        elif self.backend in ("xgboost", "xgboost_gpu"):
            # Tier 1.3: ``xgboost_gpu`` reuses the XGBoostTrainer code path;
            # the device (cuda vs cpu) is selected inside ``_build_xgboost_params``
            # based on ``req.model_family``.
            return self._train_xgboost(req, deadline_ns=deadline_ns)
        else:
            raise TrainingFailure(
                error_code="invalid_backend",
                error_summary=(
                    f"unknown backend {self.backend!r}; supported: {list(TRAINER_BACKENDS)}"
                ),
            )

    def _train_catboost(
        self,
        req: RunPodTrainingRequest,
        *,
        deadline_ns: int,
    ) -> tuple[ArtifactManifest, ModelDossier]:
        """Train via CatBoostTrainer and build the artifact + dossier."""
        import tempfile

        # Lazy import — catboost_trainer itself lazy-imports catboost.
        from quant_foundry.catboost_trainer import CatBoostTrainer

        # Dependency check.
        if importlib.util.find_spec("catboost") is None:
            raise TrainingFailure(
                error_code="missing_dependency",
                error_summary="ML dependency not available: catboost",
            )
        if importlib.util.find_spec("numpy") is None:
            raise TrainingFailure(
                error_code="missing_dependency",
                error_summary="ML dependency not available: numpy",
            )

        # Load dataset (reuse the existing numpy-array loader).
        if self.fold_spec is not None:
            X, y, timestamps, weights, groups, _fa = self._load_dataset_with_folds(
                req.dataset_manifest_ref
            )
        else:
            X, y, timestamps, weights, groups = self._load_dataset(
                req.dataset_manifest_ref,
            )

        if time.time_ns() >= deadline_ns:
            raise TrainingFailure(
                error_code="timeout",
                error_summary="training deadline breached after dataset load (catboost)",
            )

        # Build CatBoost params from the request search space.
        seed = req.random_seed if req.random_seed is not None else 0
        cb_params = self._build_catboost_params(req, seed)

        # Save artifact to a temp .cbm file.
        tmp_dir = tempfile.mkdtemp(prefix="qf_catboost_")
        artifact_path = os.path.join(tmp_dir, "model.cbm")

        assert self.column_roles is not None  # validated before dispatch
        assert self.task_spec is not None  # validated before dispatch
        trainer = CatBoostTrainer(
            column_roles=self.column_roles,
            task_spec=self.task_spec,
            params=cb_params,
            artifact_path=artifact_path,
            strict_gpu=False,  # canary: allow CPU fallback
            n_folds=self.n_folds,
            random_seed=seed,
        )

        # CatBoost requires group ids to be string or integral (not
        # float). Convert the groups array to int when present.
        cb_groups = groups
        if cb_groups is not None:
            import numpy as np

            cb_groups = np.asarray(cb_groups)
            if cb_groups.dtype.kind == "f":
                cb_groups = cb_groups.astype(np.int64)

        try:
            result = trainer.train(X, y, weights=weights, groups=cb_groups)
        except ImportError as exc:
            raise TrainingFailure(
                error_code="missing_dependency",
                error_summary=f"catboost training failed: {exc}",
            ) from exc
        except Exception as exc:
            raise TrainingFailure(
                error_code="training_error",
                error_summary=f"catboost training failed: {exc}",
            ) from exc

        if time.time_ns() >= deadline_ns:
            raise TrainingFailure(
                error_code="timeout",
                error_summary="training deadline breached after catboost fit",
            )

        # Read the saved artifact bytes.
        model_bytes = b""
        if os.path.exists(artifact_path):
            with open(artifact_path, "rb") as fh:
                model_bytes = fh.read()
        if not model_bytes:
            # Fallback: pickle the model.
            model_bytes = pickle.dumps(
                result.model,
                protocol=pickle.HIGHEST_PROTOCOL,
            )

        # Compute metrics from in-sample predictions.
        metrics = self._compute_backend_metrics(
            result.model,
            X,
            y,
            weights,
            groups,
            timestamps,
            req,
            is_catboost=True,
        )

        # T-8.2: rank metrics for ranking tasks.
        rank_report = self._maybe_compute_rank_metrics(metrics, groups, timestamps)
        if rank_report is not None:
            metrics["rank_report"] = rank_report

        return self._build_backend_artifact_and_dossier(
            req,
            model_bytes,
            metrics,
            result.n_features,
            result.n_rows,
            artifact_format="catboost-cbm",
            loader_family="catboost",
            trainer_tag="real_catboost",
            rank_report=rank_report,
            # Tier 1.3: catboost_gpu trains on GPU (non-deterministic
            # floating-point summation order); plain catboost trains on
            # CPU (deterministic reference baseline alongside LightGBM CPU).
            determinism_status=(
                "non_deterministic" if req.model_family == "catboost_gpu" else "deterministic"
            ),
            # Tier 1.3: record the GPU model when training on GPU.
            gpu_model=_probe_gpu_model() if req.model_family == "catboost_gpu" else None,
        )

    def _train_xgboost(
        self,
        req: RunPodTrainingRequest,
        *,
        deadline_ns: int,
    ) -> tuple[ArtifactManifest, ModelDossier]:
        """Train via XGBoostTrainer and build the artifact + dossier."""
        import tempfile

        from quant_foundry.xgboost_trainer import XGBoostTrainer

        # Dependency check.
        if importlib.util.find_spec("xgboost") is None:
            raise TrainingFailure(
                error_code="missing_dependency",
                error_summary="ML dependency not available: xgboost",
            )
        if importlib.util.find_spec("numpy") is None:
            raise TrainingFailure(
                error_code="missing_dependency",
                error_summary="ML dependency not available: numpy",
            )

        # Load dataset.
        if self.fold_spec is not None:
            X, y, timestamps, weights, groups, _fa = self._load_dataset_with_folds(
                req.dataset_manifest_ref
            )
        else:
            X, y, timestamps, weights, groups = self._load_dataset(
                req.dataset_manifest_ref,
            )

        if time.time_ns() >= deadline_ns:
            raise TrainingFailure(
                error_code="timeout",
                error_summary="training deadline breached after dataset load (xgboost)",
            )

        # Build XGBoost params from the request search space.
        seed = req.random_seed if req.random_seed is not None else 0
        xgb_params = self._build_xgboost_params(req, seed)

        # Save artifact to a temp .ubj file.
        tmp_dir = tempfile.mkdtemp(prefix="qf_xgboost_")
        artifact_path = os.path.join(tmp_dir, "model.ubj")

        assert self.column_roles is not None  # validated before dispatch
        assert self.task_spec is not None  # validated before dispatch
        trainer = XGBoostTrainer(
            column_roles=self.column_roles,
            task_spec=self.task_spec,
            params=xgb_params,
            artifact_path=artifact_path,
            strict=False,  # canary: allow CPU fallback
            n_folds=self.n_folds,
            random_seed=seed,
        )

        try:
            result = trainer.train(X, y, weights=weights, groups=groups)
        except ImportError as exc:
            raise TrainingFailure(
                error_code="missing_dependency",
                error_summary=f"xgboost training failed: {exc}",
            ) from exc
        except Exception as exc:
            raise TrainingFailure(
                error_code="training_error",
                error_summary=f"xgboost training failed: {exc}",
            ) from exc

        if time.time_ns() >= deadline_ns:
            raise TrainingFailure(
                error_code="timeout",
                error_summary="training deadline breached after xgboost fit",
            )

        # Read the saved artifact bytes.
        model_bytes = b""
        if os.path.exists(artifact_path):
            with open(artifact_path, "rb") as fh:
                model_bytes = fh.read()
        if not model_bytes:
            model_bytes = pickle.dumps(
                result.model,
                protocol=pickle.HIGHEST_PROTOCOL,
            )

        # Compute metrics from in-sample predictions.
        metrics = self._compute_backend_metrics(
            result.model,
            X,
            y,
            weights,
            groups,
            timestamps,
            req,
            is_catboost=False,
            xgb_result=result,
        )

        # T-8.2: rank metrics for ranking tasks.
        rank_report = self._maybe_compute_rank_metrics(metrics, groups, timestamps)
        if rank_report is not None:
            metrics["rank_report"] = rank_report

        return self._build_backend_artifact_and_dossier(
            req,
            model_bytes,
            metrics,
            n_features=len(self.column_roles.feature_columns) if self.column_roles else 0,
            n_rows=int(X.shape[0]) if hasattr(X, "shape") else len(y),
            artifact_format="xgboost-ubj",
            loader_family="xgboost",
            trainer_tag="real_xgboost",
            rank_report=rank_report,
            # Tier 1.3: xgboost_gpu trains on CUDA (non-deterministic GPU
            # floating-point summation order); plain xgboost trains on CPU
            # (deterministic reference baseline alongside LightGBM CPU).
            determinism_status=(
                "non_deterministic" if req.model_family == "xgboost_gpu" else "deterministic"
            ),
            # Tier 1.3: record the GPU model when training on CUDA so
            # the registry/gate can group same-GPU-family runs.
            gpu_model=_probe_gpu_model() if req.model_family == "xgboost_gpu" else None,
        )

    # --- backend param builders -----------------------------------------

    def _build_catboost_params(
        self,
        req: RunPodTrainingRequest,
        seed: int,
    ) -> dict[str, Any]:
        """Build CatBoost hyper-parameters from the request search space.

        Tier 1.3: When ``req.model_family == 'catboost_gpu'`` the
        ``task_type`` is set to ``'GPU'`` to train on GPU; otherwise
        it stays ``'CPU'`` (the deterministic reference). GPU CatBoost
        is non-deterministic (floating-point summation order differs
        from CPU) and is flagged as such in the artifact manifest.
        """
        ss = req.search_space
        is_gpu = req.model_family == "catboost_gpu"
        params: dict[str, Any] = {
            "iterations": int(ss.get("n_estimators", [100])[0]),
            "depth": int(ss.get("max_depth", [6])[0]),
            "learning_rate": float(ss.get("learning_rate", [0.1])[0]),
            "random_seed": seed,
            "verbose": False,
            "allow_writing_files": False,
            "task_type": "GPU" if is_gpu else "CPU",
        }
        if ss.get("num_leaves"):
            params["num_leaves"] = int(ss["num_leaves"][0])
        if ss.get("l2_leaf_reg"):
            params["l2_leaf_reg"] = float(ss["l2_leaf_reg"][0])
        return params

    def _build_xgboost_params(
        self,
        req: RunPodTrainingRequest,
        seed: int,
    ) -> dict[str, Any]:
        """Build XGBoost hyper-parameters from the request search space.

        Tier 1.3: When ``req.model_family == 'xgboost_gpu'`` the ``device``
        is set to ``'cuda'`` to train on GPU; otherwise it stays ``'cpu'``.
        GPU training is non-deterministic (floating-point summation order
        differs from CPU) and is flagged as such in the artifact manifest
        by :meth:`_build_backend_artifact_and_dossier`.
        """
        ss = req.search_space
        # Tier 1.3: select the XGBoost device from the model family. The
        # ``xgboost_gpu`` family trains on CUDA; every other family (incl.
        # plain ``xgboost``) trains on CPU (the deterministic reference).
        is_gpu = req.model_family == "xgboost_gpu"
        params: dict[str, Any] = {
            "tree_method": "hist",
            "device": "cuda" if is_gpu else "cpu",
            "max_depth": int(ss.get("max_depth", [3])[0]),
            "learning_rate": float(ss.get("learning_rate", [0.1])[0]),
            "n_estimators": int(ss.get("n_estimators", [50])[0]),
        }
        if ss.get("subsample"):
            params["subsample"] = float(ss["subsample"][0])
        if ss.get("colsample_bytree"):
            params["colsample_bytree"] = float(ss["colsample_bytree"][0])
        if ss.get("min_child_weight"):
            params["min_child_weight"] = float(ss["min_child_weight"][0])
        return params

    # --- backend metrics + artifact -------------------------------------

    def _compute_backend_metrics(
        self,
        model: Any,
        X: Any,
        y: Any,
        weights: Any,
        groups: Any,
        timestamps: Any,
        req: RunPodTrainingRequest,
        *,
        is_catboost: bool,
        xgb_result: Any = None,
    ) -> dict[str, Any]:
        """Compute standard metrics from a backend model's predictions.

        For catboost: uses ``model.predict`` / ``predict_proba``.
        For xgboost: builds a DMatrix and uses ``model.predict``.
        """
        import numpy as np

        task_type = self.task_spec.task_type if self.task_spec else "binary"

        preds: Any = None
        if is_catboost:
            try:
                if task_type in ("binary", "multiclass") and hasattr(model, "predict_proba"):
                    proba = model.predict_proba(X)
                    proba_arr = np.asarray(proba)
                    preds = proba_arr[:, -1] if proba_arr.ndim == 2 else proba_arr
                else:
                    preds = np.asarray(model.predict(X), dtype=np.float64)
            except Exception:
                preds = np.full(len(y), 0.5, dtype=np.float64)
        else:
            # xgboost
            try:
                import xgboost as xgb

                feature_names = list(self.column_roles.feature_columns) if self.column_roles else []
                dmat = xgb.DMatrix(X, label=y, feature_names=feature_names)
                preds = np.asarray(model.predict(dmat), dtype=np.float64)
            except Exception:
                preds = np.full(len(y), 0.5, dtype=np.float64)

        preds_arr = np.asarray(preds, dtype=np.float64).ravel()
        labels_arr = np.asarray(y, dtype=np.float64).ravel()

        # Use the shared metrics computer.
        periods_per_year = self._resolve_periods_per_year(req.extra_constraints)
        result = self._compute_metrics(
            preds_arr,
            labels_arr,
            [],
            [],
            periods_per_year=periods_per_year,
        )
        # Stash OOS data for rank metrics (in-sample for backends).
        result["_oos_preds"] = preds_arr
        result["_oos_labels"] = labels_arr
        result["_oos_groups"] = np.asarray(groups) if groups is not None else None
        result["_oos_timestamps"] = np.asarray(timestamps) if timestamps is not None else None
        return result

    def _build_backend_artifact_and_dossier(
        self,
        req: RunPodTrainingRequest,
        model_bytes: bytes,
        metrics: dict[str, Any],
        n_features: int,
        n_rows: int,
        *,
        artifact_format: str,
        loader_family: str,
        trainer_tag: str,
        rank_report: Any = None,
        determinism_status: str | None = None,
        gpu_model: str | None = None,
    ) -> tuple[ArtifactManifest, ModelDossier]:
        """Build the artifact manifest + dossier for a backend training.

        Tier 1.3: ``determinism_status`` records whether the training was
        deterministic (``"deterministic"`` for CPU backends) or
        non-deterministic (``"non_deterministic"`` for GPU backends whose
        floating-point summation order differs from CPU). ``None`` keeps
        the manifest backward compatible with pre-existing artifacts.
        ``gpu_model`` records the GPU model used (from ``nvidia-smi``),
        or ``None`` for CPU training. The registry/gate uses this to
        group "deterministic within this GPU family" vs "non-deterministic
        across GPU families" for XGBoost GPU.
        """
        sha256 = hashlib.sha256(model_bytes).hexdigest()
        size_bytes = len(model_bytes)

        feature_schema_hash = hashlib.sha256(
            f"{req.dataset_manifest_ref}:n_features={n_features}".encode(),
        ).hexdigest()[:16]
        label_schema_hash = hashlib.sha256(
            f"{req.dataset_manifest_ref}:label={self.task_spec.task_type if self.task_spec else 'unknown'}".encode(),
        ).hexdigest()[:16]

        now_ns = time.time_ns()
        artifact_id = f"artifact:{sha256[:16]}"
        artifact = ArtifactManifest(
            artifact_id=artifact_id,
            sha256=sha256,
            size_bytes=size_bytes,
            uri=None,
            model_family=req.model_family,
            created_at_ns=now_ns,
            feature_schema_hash=feature_schema_hash,
            label_schema_hash=label_schema_hash,
            code_git_sha=_git_sha_or_default(),
            lockfile_hash=_lockfile_hash_or_default(),
            container_image_digest=_container_digest_or_default(),
            determinism_status=determinism_status,
            gpu_model=gpu_model,
        )

        try:
            typed_result = build_artifact_result(
                artifact_id=artifact_id,
                model_bytes=model_bytes,
                model_family=req.model_family,
                req=req,
                artifact_uri=None,
                artifact_format=artifact_format,
                artifact_kind="model",
                loader_family=loader_family,
                created_at=now_ns,
            )
        except ValueError as exc:
            raise TrainingFailure(
                error_code="artifact_missing",
                error_summary=(
                    f"successful training produced no artifact bytes (fail closed): {exc}"
                ),
            ) from exc
        self.last_artifact_result = typed_result
        self.last_model_bytes = model_bytes

        dossier = ModelDossier(
            model_id=f"model:{req.job_id}",
            artifact_manifest_id=artifact.artifact_id,
            dataset_manifest_id=req.dataset_manifest_ref,
            code_git_sha=artifact.code_git_sha or "unknown",
            lockfile_hash=artifact.lockfile_hash or "unknown",
            container_image_digest=artifact.container_image_digest or "unknown",
            random_seed=req.random_seed,
            hardware_class=req.hardware_class,
            training_metrics=metrics["training_metrics"],
            pbo=metrics["pbo"],
            deflated_sharpe=metrics["deflated_sharpe"],
            authority=Authority.SHADOW_ONLY,
            metadata={
                "model_family": req.model_family,
                "trainer": trainer_tag,
                "backend": self.backend,
                "n_features": str(n_features),
                "n_rows": str(n_rows),
                "n_folds": str(self.n_folds),
                "brier_score": str(metrics["brier_score"]),
                "win_rate": str(metrics["win_rate"]),
                "max_drawdown": str(metrics["max_drawdown"]),
                "sharpe_ratio": str(metrics["sharpe_ratio"]),
                "pbo_method": metrics.get("pbo_method", "fold_overfit_ratio"),
                "deflated_sharpe_method": metrics.get(
                    "deflated_sharpe_method",
                    "sharpe_times_1_minus_fold_overfit_ratio",
                ),
                "fold_source": "manifest" if self.fold_spec else "heuristic",
                "has_rank_report": str(rank_report is not None),
                # Tier 2.1/2.2: DSR + PBO detail fields for transparency.
                "deflated_sharpe_raw": str(metrics.get("deflated_sharpe_raw", "")),
                "deflated_sharpe_trial_count": str(metrics.get("deflated_sharpe_trial_count", "")),
                "deflated_sharpe_skew": str(metrics.get("deflated_sharpe_skew", "")),
                "deflated_sharpe_kurtosis": str(metrics.get("deflated_sharpe_kurtosis", "")),
                "deflated_sharpe_multiple_trials_penalty": str(
                    metrics.get("deflated_sharpe_multiple_trials_penalty", "")
                ),
                "deflated_sharpe_non_normality_penalty": str(
                    metrics.get("deflated_sharpe_non_normality_penalty", "")
                ),
                "pbo_logit": str(metrics.get("pbo_logit", "")),
                "pbo_n_combinations": str(metrics.get("pbo_n_combinations", "")),
                "pbo_flagged": str(metrics.get("pbo_flagged", "")),
                "cv_mode": str(metrics.get("cv_mode", "walk_forward")),
                "cpcv_n_groups": str(metrics.get("cpcv_n_groups", "")),
                "cpcv_n_val_groups": str(metrics.get("cpcv_n_val_groups", "")),
                "cpcv_n_folds": str(metrics.get("cpcv_n_folds", "")),
                # Tier 2.3: triple-barrier label config (when set).
                "barrier_config": str(self.task_spec.barrier_config if self.task_spec else None),
                # Tier 2.5: execution-aware (net-of-cost) metrics.
                "sharpe_net": str(metrics.get("sharpe_net", "")),
                "max_drawdown_net": str(metrics.get("max_drawdown_net", "")),
                "win_rate_net": str(metrics.get("win_rate_net", "")),
                "turnover": str(metrics.get("turnover", "")),
                "total_cost_bps": str(metrics.get("total_cost_bps", "")),
                "cost_model_version": str(metrics.get("cost_model_version", "")),
            },
        )
        return artifact, dossier

    # --- T-8.2: rank metrics --------------------------------------------

    def _maybe_compute_rank_metrics(
        self,
        metrics: dict[str, Any],
        groups: Any,
        timestamps: Any,
    ) -> Any:
        """Compute rank metrics when task_type == 'ranking' and groups exist.

        Returns a :class:`RankReport` or ``None`` when the task is not
        ranking or groups are unavailable.
        """
        if self.task_spec is None or self.task_spec.task_type != "ranking":
            return None
        # Lazy import — rank_metrics imports numpy at module level.
        import numpy as np

        from quant_foundry.rank_metrics import compute_rank_metrics

        preds = metrics.get("_oos_preds")
        labels = metrics.get("_oos_labels")
        oos_groups = metrics.get("_oos_groups")
        oos_ts = metrics.get("_oos_timestamps")

        # Prefer the OOS groups collected during validation; fall back
        # to the full-dataset groups (for backend in-sample predictions).
        grp = oos_groups if oos_groups is not None else groups
        ts = oos_ts if oos_ts is not None else timestamps

        if preds is None or labels is None or grp is None:
            return None

        preds_arr = np.asarray(preds, dtype=np.float64).ravel()
        labels_arr = np.asarray(labels, dtype=np.float64).ravel()
        grp_arr = np.asarray(grp).ravel()
        ts_arr = np.asarray(ts).ravel() if ts is not None else None

        if preds_arr.shape[0] == 0 or grp_arr.shape[0] == 0:
            return None

        try:
            return compute_rank_metrics(
                preds_arr,
                labels_arr,
                grp_arr,
                ts_arr,
                top_k=10,
                cost_per_turnover=0.001,
            )
        except (ValueError, TypeError):
            return None

    # --- T-8.4: manifest fold dataset loading ---------------------------

    def _load_dataset_with_folds(
        self,
        ref: str,
    ) -> tuple[Any, Any, Any, Any, Any, Any]:
        """Load a dataset + consume manifest folds.

        Returns ``(X, y, timestamps, weights, groups, fold_assignment)``
        where the numpy arrays are in original dataframe row order (NOT
        time-sorted) so the manifest fold indices align correctly.
        """

        from quant_foundry.fold_consumer import consume_manifest_folds

        df = self._load_dataframe(ref)
        X, y, timestamps, weights, groups = self._extract_arrays_from_df(df)

        # Consume manifest folds using the dataframe.
        assert self.fold_spec is not None  # validated before dataset load
        fold_assignment = consume_manifest_folds(
            self.fold_spec,
            df,
        )
        return X, y, timestamps, weights, groups, fold_assignment

    def _load_dataframe(self, ref: str) -> Any:
        """Load a dataset as a pandas DataFrame (for manifest fold consumption).

        Supports ``.parquet`` and ``.csv``. The dataframe retains all
        columns (features, labels, timestamp, weights, groups, row-id
        columns) so ``consume_manifest_folds`` can extract row keys.
        """
        path = self._resolve_path(ref)
        if not path.exists():
            raise TrainingFailure(
                error_code="dataset_not_found",
                error_summary=f"dataset file not found: {path}",
            )
        try:
            import pandas as pd
        except ImportError as exc:
            raise TrainingFailure(
                error_code="missing_dependency",
                error_summary="pandas is required for manifest fold consumption",
            ) from exc

        ext = path.suffix.lower()
        if ext == ".parquet":
            return pd.read_parquet(str(path))
        elif ext == ".csv":
            return pd.read_csv(str(path))
        else:
            raise TrainingFailure(
                error_code="unsupported_format",
                error_summary=f"unsupported dataset format: {ext} (expected .parquet or .csv)",
            )

    def _extract_arrays_from_df(
        self,
        df: Any,
    ) -> tuple[Any, Any, Any, Any, Any]:
        """Extract (X, y, timestamps, weights, groups) from a DataFrame.

        Uses ``column_roles`` to select feature / label / timestamp /
        weight / group columns. Falls back to the legacy infer-by-
        dropping-names behaviour when ``column_roles`` is None.
        """
        import numpy as np

        columns = list(df.columns)
        available = set(columns)

        if self.column_roles is not None:
            roles = self.column_roles
            # Label.
            if self.task_spec is not None and self.task_spec.label_column:
                label_col = self.task_spec.label_column
            else:
                label_col = roles.primary_label
            # Features.
            feature_cols = list(roles.feature_columns)
            # Timestamp.
            ts_col = roles.timestamp_column
            # Weights.
            weights = None
            if roles.weight_column is not None:
                weights = np.array(
                    df[roles.weight_column].values,
                    dtype=np.float64,
                )
            # Groups.
            groups = None
            if roles.group_column is not None:
                groups = np.array(df[roles.group_column].values)
        else:
            label_col = "label" if "label" in columns else columns[-1]
            ts_col = None
            for candidate in ("timestamp", "decision_time", "ts", "event_ts"):
                if candidate in columns:
                    ts_col = candidate
                    break
            feature_cols = [c for c in columns if c != label_col and c != ts_col]
            weights = None
            groups = None

        y = np.array(df[label_col].values, dtype=np.float64)
        X = np.column_stack(
            [np.array(df[c].values, dtype=np.float64) for c in feature_cols],
        )

        if ts_col is not None and ts_col in available:
            # Handle both numeric and string (ISO date) timestamps.
            try:
                timestamps = np.array(
                    df[ts_col].values,
                    dtype=np.int64,
                )
            except (ValueError, TypeError):
                # String timestamps (e.g. "2024-01-15") — convert via
                # pandas to datetime, then to int64 epoch nanoseconds.
                import pandas as pd

                timestamps = np.array(
                    pd.to_datetime(df[ts_col].values).astype("int64"),
                    dtype=np.int64,
                )
        else:
            timestamps = np.arange(len(y), dtype=np.int64)

        return X, y, timestamps, weights, groups

    # --- T-7.1: artifact loading via artifact_io ------------------------

    def load_model(self, path: str, *, backend: str | None = None) -> Any:
        """Load a saved model artifact using the appropriate artifact_io loader.

        The loader is selected based on the ``backend`` argument (or the
        trainer's ``self.backend`` when None):

        - ``"lightgbm"`` -> :func:`load_lightgbm_model`
        - ``"catboost"`` -> :func:`load_catboost_model`
        - ``"xgboost"`` -> :func:`load_xgboost_model`

        Raises:
            TrainingFailure: if the backend is unknown or the loader
                fails.
        """
        from quant_foundry.artifact_io import (
            load_catboost_model,
            load_lightgbm_model,
            load_xgboost_model,
        )

        eff_backend = backend if backend is not None else self.backend
        loaders = {
            "lightgbm": load_lightgbm_model,
            "catboost": load_catboost_model,
            "xgboost": load_xgboost_model,
        }
        if eff_backend not in loaders:
            raise TrainingFailure(
                error_code="invalid_backend",
                error_summary=(
                    f"unknown backend {eff_backend!r} for artifact loading; "
                    f"supported: {sorted(loaders)}"
                ),
            )
        try:
            return loaders[eff_backend](path)
        except Exception as exc:
            raise TrainingFailure(
                error_code="artifact_load_failed",
                error_summary=f"failed to load artifact from {path!r}: {exc}",
            ) from exc
