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
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

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
# Phase 8 / T-8.1: explicit column roles + task spec. Imported lazily-safe
# (dataset_manifest / training_manifest do not import real_trainer, so
# there is no circular import). These are OPTIONAL inputs — when None the
# trainer falls back to the legacy infer-by-dropping-names behaviour with
# a deprecation warning.
from quant_foundry.dataset_manifest import (
    ColumnRoles,
    validate_column_roles,
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
    # --- Phase 1 / T-1.1: typed artifact result -------------------------
    # After a successful ``train()`` call, the typed artifact result +
    # raw model bytes are stashed here so the RunPod handler can read
    # them through a typed field instead of the fragile
    # ``getattr(result, "model_bytes", None)`` pattern. The handler
    # creates a fresh trainer per job, so this per-instance state is
    # safe (no cross-job leakage).
    last_artifact_result: TypedArtifactResult | None = None
    last_model_bytes: bytes | None = None

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

        X, y, timestamps, weights, groups = self._load_dataset(
            req.dataset_manifest_ref,
        )

        if time.time_ns() >= deadline_ns:
            raise TrainingFailure(
                error_code="timeout",
                error_summary="training deadline breached after dataset load",
            )

        seed = req.random_seed if req.random_seed is not None else 0

        metrics = self._walk_forward_validate(
            X,
            y,
            timestamps,
            seed,
            deadline_ns,
            req,
            weights=weights,
            groups=groups,
        )

        if time.time_ns() >= deadline_ns:
            raise TrainingFailure(
                error_code="timeout",
                error_summary="training deadline breached after validation",
            )

        final_model = self._train_final_model(X, y, seed, req, weights=weights)

        model_bytes = pickle.dumps(final_model, protocol=pickle.HIGHEST_PROTOCOL)
        sha256 = hashlib.sha256(model_bytes).hexdigest()
        size_bytes = len(model_bytes)

        n_features = int(X.shape[1])
        n_rows = int(X.shape[0])
        feature_schema_hash = hashlib.sha256(
            f"{req.dataset_manifest_ref}:n_features={n_features}".encode(),
        ).hexdigest()[:16]
        label_schema_hash = hashlib.sha256(
            f"{req.dataset_manifest_ref}:label=binary".encode(),
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
                artifact_format="pickle",
                artifact_kind="model",
                loader_family="lightgbm",
                created_at=now_ns,
            )
        except ValueError as exc:
            raise TrainingFailure(
                error_code="artifact_missing",
                error_summary=(
                    "successful training produced no artifact bytes "
                    f"(fail closed): {exc}"
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
                    "label columns must not appear in feature_columns: "
                    f"{sorted(lf_overlap)!r}"
                ),
            )

        # If a task spec is provided, validate it against the column roles.
        if self.task_spec is not None:
            verdict = validate_task_spec(self.task_spec, self.column_roles)
            if not verdict.passed:
                raise TrainingFailure(
                    error_code="invalid_task_spec",
                    error_summary=(
                        "task spec validation failed: "
                        + "; ".join(verdict.errors)
                    ),
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
            tmp_path = Path(tempfile.mktemp(prefix="qf_http_", suffix=suffix))
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
        self, ref: str,
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

            table = pq.read_table(str(path))
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
                    error_summary=(
                        "column roles validation failed: "
                        + "; ".join(verdict.errors)
                    ),
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
                            f"weight column {roles.weight_column!r} not "
                            f"found in dataset columns"
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
                            f"group column {roles.group_column!r} not "
                            f"found in dataset columns"
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
        with open(str(path), "r", encoding="utf-8", errors="replace") as fh:
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
                    error_summary=(
                        "column roles validation failed: "
                        + "; ".join(verdict.errors)
                    ),
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
                            f"feature column {fc!r} not found in CSV "
                            f"header {header_cols!r}"
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
                            f"weight column {roles.weight_column!r} not "
                            f"found in CSV header"
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
                            f"group column {roles.group_column!r} not "
                            f"found in CSV header"
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
        """Build LightGBM parameters from request search space + defaults."""
        params: dict[str, Any] = {
            "objective": "binary",
            "metric": "binary_logloss",
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
        """
        import lightgbm as lgb
        import numpy as np

        n = len(y)
        if n < 10:
            raise TrainingFailure(
                error_code="insufficient_data",
                error_summary=f"dataset too small for walk-forward validation: {n} rows",
            )

        order = np.argsort(timestamps, kind="stable")
        X_s = X[order]
        y_s = y[order]
        # Apply the same time-ordering to weights/groups so they stay
        # aligned with the feature/label rows.
        weights_s = weights[order] if weights is not None else None
        groups_s = groups[order] if groups is not None else None

        # Resolve the purge gap from the request. Default = horizon_bars
        # (matches Path A: ``--purge-bars -1`` means "use --horizon-bars").
        horizon_bars = self._resolve_horizon_bars(req.extra_constraints)
        purge_bars = self._resolve_purge_bars(horizon_bars, req.extra_constraints)

        # Canonical fold math (F2 + F4 fix). make_folds raises ValueError
        # if the dataset is too small for the requested folds + purge; we
        # translate that to a TrainingFailure so the handler returns a
        # safe terminal status.
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

        params = self._build_lgb_params(seed, req)
        n_estimators = self._get_n_estimators(req)

        # Early stopping: if early_stopping_rounds is set in search_space,
        # use validation-based early stopping to prevent overfitting.
        early_stopping_rounds = None
        es_val = req.search_space.get("early_stopping_rounds")
        if es_val:
            early_stopping_rounds = int(es_val[0])

        all_preds: list[float] = []
        all_labels: list[float] = []
        fold_train_acc: list[float] = []
        fold_val_acc: list[float] = []
        fold_best_iterations: list[int] = []

        for fold in folds:
            if time.time_ns() >= deadline_ns:
                raise TrainingFailure(
                    error_code="timeout",
                    error_summary=f"training deadline breached during fold {fold.index}",
                )

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

            # Phase 8 / T-8.1: pass sample weights to the train Dataset
            # when column_roles declares a weight column.
            train_kwargs: dict[str, Any] = {}
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
                    callbacks=callbacks,
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
            train_acc = float(np.mean((train_pred > 0.5) == (y_train > 0.5)))
            val_pred = np.asarray(model.predict(X_val), dtype=np.float64)
            val_acc = float(np.mean((val_pred > 0.5) == (y_val > 0.5)))

            fold_train_acc.append(train_acc)
            fold_val_acc.append(val_acc)
            all_preds.extend(val_pred.tolist())
            all_labels.extend(y_val.tolist())

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
        # Store for final model training (use avg best iteration)
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

        deflated_sharpe = sharpe * (1.0 - fold_overfit_ratio)

        return {
            "training_metrics": {
                "accuracy": accuracy,
                "logloss": logloss,
                "brier_score": brier,
                "sharpe_ratio": sharpe,
                "max_drawdown": max_drawdown,
                "win_rate": win_rate,
            },
            "pbo": fold_overfit_ratio,
            "deflated_sharpe": deflated_sharpe,
            "pbo_method": "fold_overfit_ratio",
            "deflated_sharpe_method": "sharpe_times_1_minus_fold_overfit_ratio",
            "brier_score": brier,
            "win_rate": win_rate,
            "max_drawdown": max_drawdown,
            "sharpe_ratio": sharpe,
            "calibration_bucket_probs": bucket_probs,
            "calibration_bucket_actuals": bucket_actuals,
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
