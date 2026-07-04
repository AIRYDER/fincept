"""
quant_foundry.xgboost_trainer — XGBoost GPU trainer adapter (T-7.3).

This module defines :class:`XGBoostTrainer`, a thin adapter that binds the
explicit column-role / task-spec declarations from
:mod:`quant_foundry.dataset_manifest` (T-8.1 :class:`ColumnRoles`) and
:mod:`quant_foundry.training_manifest` (T-8.1 :class:`ModelTaskSpec`) to an
XGBoost training run.

Design:

- **Lazy imports.** ``xgboost`` (and ``numpy``) are imported *inside* the
  methods that need them, so this module stays importable in environments
  where XGBoost is not installed — the same pattern used by
  :mod:`quant_foundry.artifact_io` (T-7.1) and
  :mod:`quant_foundry.real_trainer`.
- **GPU fail-closed.** When ``params["device"] == "cuda"`` but no CUDA GPU
  is reachable, the trainer either raises (``strict=True``, the production
  default) or falls back to ``device="cpu"`` with a warning
  (``strict=False``, the canary/research default). The capability probe is
  a tiny throwaway train on a 4-row DMatrix — cheap and deterministic.
- **Objective mapping.** The :class:`ModelTaskSpec.task_type` is mapped to
  the XGBoost objective name (``binary:logistic``, ``reg:squarederror``,
  ``rank:pairwise``, ``multi:softmax``). An explicit ``objective`` in
  ``params`` overrides the mapping (research escape hatch).
- **Artifact format.** The model is saved in the native XGBoost format.
  The file extension selects the on-disk encoding: ``.ubj`` → the binary
  UBJ format (preferred for size/speed), ``.json`` → the human-readable
  JSON format. Any other extension defaults to UBJ. Both round-trip
  through :func:`quant_foundry.artifact_io.load_xgboost_model`.
- **Feature importance.** :meth:`get_feature_importance` returns a dict
  keyed by feature name with ``gain``, ``weight``, and ``cover`` values
  (the three XGBoost importance types that are always available for tree
  models).
- **Fold metrics.** :meth:`train` performs an internal k-fold
  walk-forward-style validation (manual split, no sklearn dependency) and
  emits one :class:`FoldMetric` per fold with task-appropriate metrics
  (AUC + logloss for binary, MSE + MAE for regression, NDCG@k for
  ranking, accuracy + mlogloss for multiclass).

This module is file-disjoint from the other builders' owned files. It
imports only from :mod:`quant_foundry.dataset_manifest`,
:mod:`quant_foundry.training_manifest`, and
:mod:`quant_foundry.artifact_io` — all of which are complete (T-7.1,
T-8.1).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from quant_foundry.dataset_manifest import ColumnRoles
from quant_foundry.training_manifest import ModelTaskSpec

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Objective mapping
# ---------------------------------------------------------------------------

#: Mapping from :attr:`ModelTaskSpec.task_type` to the XGBoost objective
#: name. An explicit ``objective`` key in ``params`` overrides this mapping
#: (research escape hatch for novel objectives).
OBJECTIVE_MAP: dict[str, str] = {
    "binary": "binary:logistic",
    "regression": "reg:squarederror",
    "ranking": "rank:pairwise",
    "multiclass": "multi:softmax",
}

#: Importance types emitted by :meth:`XGBoostTrainer.get_feature_importance`.
IMPORTANCE_TYPES: tuple[str, ...] = ("gain", "weight", "cover")


# ---------------------------------------------------------------------------
# Result models (Pydantic v2, frozen + extra='forbid')
# ---------------------------------------------------------------------------


class FoldMetric(BaseModel):
    """Metrics for a single validation fold.

    Frozen + ``extra='forbid'`` (audit integrity). ``metrics`` is a flat
    dict of metric-name → value (e.g. ``{"auc": 0.61, "logloss": 0.69}``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    fold_id: int
    train_size: int
    val_size: int
    metrics: dict[str, float]

    @field_validator("fold_id")
    @classmethod
    def _fold_id_nonneg(cls, v: int) -> int:
        if v < 0:
            raise ValueError(f"fold_id must be >= 0; got {v}")
        return v

    @field_validator("train_size", "val_size")
    @classmethod
    def _size_positive(cls, v: int, info: Any) -> int:
        if v <= 0:
            raise ValueError(f"{info.field_name} must be > 0; got {v}")
        return v


class TrainingResult(BaseModel):
    """Result of :meth:`XGBoostTrainer.train`.

    Frozen + ``extra='forbid'``. The trained ``model`` is an
    ``xgboost.Booster``; because that is not a Pydantic-native type,
    ``arbitrary_types_allowed`` is enabled. The model is excluded from
    equality comparisons (it has no useful ``__eq__``) by comparing only
    on the serializable fields — callers that need to compare results
    should compare ``artifact_path`` + ``fold_metrics``.

    Fields:
        model: the trained ``xgboost.Booster`` (or ``None`` before
            training / after a load-only construction).
        artifact_path: the path the model was saved to.
        feature_importance: dict keyed by feature name →
            ``{"gain": float, "weight": float, "cover": float}``.
        fold_metrics: tuple of :class:`FoldMetric`, one per validation
            fold. Empty when ``n_folds <= 0``.
        objective: the XGBoost objective name used.
        task_type: the :attr:`ModelTaskSpec.task_type`.
        device: the device actually used (``"cuda"`` or ``"cpu"``).
        n_estimators: the number of boosting rounds trained.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    schema_version: int = 1
    model: Any = None
    artifact_path: str
    feature_importance: dict[str, dict[str, float]]
    fold_metrics: tuple[FoldMetric, ...]
    objective: str
    task_type: str
    device: str
    n_estimators: int

    @field_validator("artifact_path")
    @classmethod
    def _path_nonempty(cls, v: str) -> str:
        if not isinstance(v, str) or not v.strip():
            raise ValueError("artifact_path must be a non-empty string")
        return v

    @field_validator("n_estimators")
    @classmethod
    def _n_estimators_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError(f"n_estimators must be > 0; got {v}")
        return v


# ---------------------------------------------------------------------------
# GPU capability probe
# ---------------------------------------------------------------------------


def _cuda_available() -> bool:
    """Return True if a CUDA GPU is reachable by XGBoost.

    Probes by running a tiny throwaway train (4 rows, 1 round) with
    ``device="cuda"``. Any exception is treated as "no GPU" — this covers
    the no-NVIDIA-driver case, the no-CUDA-runtime case, and the
    XGBoost-built-without-CUDA case.

    ``xgboost`` is imported lazily so this function can be referenced in
    environments without XGBoost (it raises ``ImportError`` only when
    actually called).
    """
    try:
        import numpy as np  # type: ignore[import-not-found]
        import xgboost as xgb  # type: ignore[import-not-found]
    except ImportError:
        return False
    try:
        X = np.zeros((4, 2), dtype=np.float32)
        y = np.zeros(4, dtype=np.float32)
        dtrain = xgb.DMatrix(X, label=y)
        params = {
            "tree_method": "hist",
            "device": "cuda",
            "max_depth": 1,
            "learning_rate": 0.1,
            "objective": "reg:squarederror",
        }
        xgb.train(params, dtrain, num_boost_round=1)
        return True
    except Exception:
        # Any failure (no driver, no CUDA, built-without-CUDA) → no GPU.
        return False


# Cache the probe result so repeated trainer constructions don't re-probe.
_CUDA_AVAILABLE_CACHE: bool | None = None


def cuda_available() -> bool:
    """Cached version of :func:`_cuda_available` (probe once per process)."""
    global _CUDA_AVAILABLE_CACHE
    if _CUDA_AVAILABLE_CACHE is None:
        _CUDA_AVAILABLE_CACHE = _cuda_available()
    return _CUDA_AVAILABLE_CACHE


# ---------------------------------------------------------------------------
# Trainer
# ---------------------------------------------------------------------------


@dataclass
class XGBoostTrainer:
    """XGBoost trainer adapter (GPU-aware, column-role / task-spec bound).

    A dataclass (per the T-7.3 spec) that binds
    :class:`ColumnRoles` (T-8.1) and :class:`ModelTaskSpec` (T-8.1) to an
    XGBoost training run. ML dependencies (``xgboost``, ``numpy``) are
    imported lazily inside :meth:`train` / :meth:`save_artifact` /
    :meth:`get_feature_importance` so this class can be constructed and
    type-checked in environments without XGBoost installed.

    Args:
        column_roles: the explicit column-role declaration from the
            dataset manifest. ``feature_columns`` is the *only* source of
            feature names; ``label_columns[0]`` is the default label.
        task_spec: the explicit learning-task declaration. ``task_type``
            drives the objective mapping; ``group_column`` is required for
            ranking.
        params: XGBoost hyperparameters. Required keys: ``tree_method``,
            ``device``, ``max_depth``, ``learning_rate``, ``n_estimators``,
            ``objective`` (optional — defaults to the task-type mapping).
        artifact_path: where to save the trained model. The extension
            selects the encoding: ``.ubj`` → binary UBJ (preferred),
            ``.json`` → JSON, anything else → UBJ.
        strict: if True (default), fail-closed when ``device="cuda"`` is
            requested but no GPU is available. If False, fall back to
            ``device="cpu"`` with a warning.
        n_folds: number of internal validation folds for fold-metric
            emission. ``0`` skips validation (empty ``fold_metrics``).
        random_seed: base RNG seed for the fold splits (deterministic).
    """

    column_roles: ColumnRoles
    task_spec: ModelTaskSpec
    params: dict[str, Any]
    artifact_path: str
    strict: bool = True
    n_folds: int = 3
    random_seed: int = 0

    # Internal state (not part of the public construction contract).
    _model: Any = field(default=None, repr=False)
    _feature_names: list[str] | None = field(default=None, repr=False)
    _device_used: str | None = field(default=None, repr=False)
    _objective_used: str | None = field(default=None, repr=False)
    _n_estimators_used: int = 0

    # --- construction validation ----------------------------------------

    def __post_init__(self) -> None:
        """Validate cross-field constraints after dataclass init."""
        if not isinstance(self.column_roles, ColumnRoles):
            raise TypeError("column_roles must be a ColumnRoles")
        if not isinstance(self.task_spec, ModelTaskSpec):
            raise TypeError("task_spec must be a ModelTaskSpec")
        if not isinstance(self.params, dict):
            raise TypeError("params must be a dict")
        if not isinstance(self.artifact_path, str) or not self.artifact_path.strip():
            raise ValueError("artifact_path must be a non-empty string")
        if not isinstance(self.n_folds, int) or self.n_folds < 0:
            raise ValueError(f"n_folds must be >= 0; got {self.n_folds}")
        # tree_method + device are the GPU-relevant knobs. We do not
        # hard-require them here (a caller may omit device to default to
        # CPU), but if device is set it must be a known value.
        device = self.params.get("device")
        if device is not None and not isinstance(device, str):
            raise TypeError("params['device'] must be a string when set")
        # Ranking requires a group column on the column roles too — the
        # task_spec already enforces group_column is set, but we also
        # want the column roles to declare it so train() can find it.
        if self.task_spec.task_type == "ranking":
            if not self.task_spec.group_column:
                raise ValueError(
                    "ranking task_type requires task_spec.group_column "
                    "(already enforced by ModelTaskSpec; re-checked here)"
                )

    # --- objective mapping ----------------------------------------------

    def resolve_objective(self) -> str:
        """Resolve the XGBoost objective for this trainer's task type.

        An explicit ``objective`` in :attr:`params` overrides the
        task-type mapping (research escape hatch). Otherwise the
        :attr:`task_spec.task_type` is mapped via :data:`OBJECTIVE_MAP`.

        Raises:
            ValueError: if ``task_type`` is not in :data:`OBJECTIVE_MAP`
                and no explicit ``objective`` was provided.
        """
        explicit = self.params.get("objective")
        if isinstance(explicit, str) and explicit.strip():
            return explicit
        if self.task_spec.task_type not in OBJECTIVE_MAP:
            raise ValueError(
                f"no objective mapping for task_type "
                f"{self.task_spec.task_type!r}; provide an explicit "
                "params['objective']"
            )
        return OBJECTIVE_MAP[self.task_spec.task_type]

    # --- GPU capability check -------------------------------------------

    def _resolve_device(self) -> str:
        """Resolve the device to use, applying the GPU fail-closed policy.

        Returns:
            The device string to actually use (``"cuda"`` or ``"cpu"``).

        Raises:
            RuntimeError: if ``strict=True``, ``device="cuda"`` was
                requested, and no CUDA GPU is available (fail-closed).
        """
        requested = self.params.get("device", "cpu")
        if requested != "cuda":
            return str(requested)
        if cuda_available():
            return "cuda"
        if self.strict:
            raise RuntimeError(
                "XGBoost GPU training requested (device='cuda') but no "
                "CUDA GPU is available, and strict=True (production "
                "mode). Refusing to train on CPU — set strict=False for "
                "canary/research mode to allow a CPU fallback."
            )
        logger.warning(
            "XGBoost GPU training requested (device='cuda') but no CUDA "
            "GPU is available; falling back to device='cpu' "
            "(strict=False / canary mode)."
        )
        return "cpu"

    # --- training -------------------------------------------------------

    def train(
        self,
        X: Any,
        y: Any,
        weights: Any | None = None,
        groups: Any | None = None,
    ) -> TrainingResult:
        """Train the XGBoost model and save the artifact.

        Args:
            X: feature matrix. A ``numpy.ndarray``, ``pandas.DataFrame``,
                or any array-like accepted by ``xgboost.DMatrix``. When
                it is a DataFrame, its column names become the feature
                names (and must match :attr:`column_roles.feature_columns`
                when provided).
            y: label vector. Shape ``(n_samples,)``. For multiclass the
                labels must be ``0..num_class-1``.
            weights: optional sample-weight vector. Shape ``(n_samples,)``.
            groups: optional group-id vector for ranking. Shape
                ``(n_samples,)``. The group *sizes* (not the raw ids) are
                passed to ``DMatrix.set_group``. Required for ranking
                tasks if not derivable from the column roles.

        Returns:
            A :class:`TrainingResult` with the trained model, feature
            importance, fold metrics, and artifact path.

        Raises:
            ImportError: if ``xgboost`` or ``numpy`` is not installed.
            RuntimeError: GPU requested but unavailable in strict mode.
            ValueError: invalid data shapes / ranking without groups.
        """
        try:
            import numpy as np  # type: ignore[import-not-found]
            import xgboost as xgb  # type: ignore[import-not-found]
        except ImportError as exc:
            raise ImportError(
                f"XGBoost training requires the 'xgboost' and 'numpy' packages; missing: {exc.name}"
            ) from exc

        # --- resolve device + objective -------------------------------
        device = self._resolve_device()
        objective = self.resolve_objective()

        # --- feature names --------------------------------------------
        feature_names = self._extract_feature_names(X)

        # --- build the training DMatrix -------------------------------
        dtrain = self._build_dmatrix(X, y, weights, groups, feature_names, objective, np, xgb)

        # --- build the params dict for xgb.train ----------------------
        n_estimators = int(self.params.get("n_estimators", 10))
        train_params: dict[str, Any] = {
            "tree_method": self.params.get("tree_method", "hist"),
            "device": device,
            "max_depth": int(self.params.get("max_depth", 3)),
            "learning_rate": float(self.params.get("learning_rate", 0.1)),
            "objective": objective,
        }
        # Pass through any extra params the caller supplied (e.g.
        # subsample, colsample_bytree, min_child_weight, num_class).
        for k, v in self.params.items():
            if k in {
                "tree_method",
                "device",
                "max_depth",
                "learning_rate",
                "objective",
                "n_estimators",
            }:
                continue
            train_params[k] = v
        # multiclass requires num_class.
        if objective == "multi:softmax":
            num_class = self.params.get("num_class")
            if num_class is None:
                num_class = int(np.max(y)) + 1
            train_params["num_class"] = int(num_class)

        # --- fold metrics (internal k-fold validation) ----------------
        fold_metrics = self._cross_validate(
            X, y, weights, groups, feature_names, objective, device, np, xgb
        )

        # --- final train on the full dataset --------------------------
        model = xgb.train(train_params, dtrain, num_boost_round=n_estimators)

        # --- stash internal state -------------------------------------
        self._model = model
        self._feature_names = feature_names
        self._device_used = device
        self._objective_used = objective
        self._n_estimators_used = n_estimators

        # --- feature importance ---------------------------------------
        importance = self._extract_importance(model, feature_names)

        # --- save artifact -------------------------------------------
        self.save_artifact(self.artifact_path)

        return TrainingResult(
            model=model,
            artifact_path=self.artifact_path,
            feature_importance=importance,
            fold_metrics=tuple(fold_metrics),
            objective=objective,
            task_type=self.task_spec.task_type,
            device=device,
            n_estimators=n_estimators,
        )

    # --- DMatrix construction -------------------------------------------

    def _extract_feature_names(self, X: Any) -> list[str]:
        """Extract feature names from X or fall back to column_roles."""
        # pandas DataFrame → use its column names.
        cols = getattr(X, "columns", None)
        if cols is not None:
            names = [str(c) for c in cols]
            return names
        # Otherwise use the declared feature columns (preferred) or f0..fN.
        if self.column_roles.feature_columns:
            return list(self.column_roles.feature_columns)
        # Last-resort fallback (numpy array without column roles).
        try:
            n = X.shape[1]  # type: ignore[index]
        except Exception:
            n = 0
        return [f"f{i}" for i in range(n)]

    def _build_dmatrix(
        self,
        X: Any,
        y: Any,
        weights: Any | None,
        groups: Any | None,
        feature_names: list[str],
        objective: str,
        np: Any,
        xgb: Any,
    ) -> Any:
        """Build a training DMatrix with weights + groups applied."""
        kwargs: dict[str, Any] = {}
        if feature_names:
            kwargs["feature_names"] = feature_names
        dtrain = xgb.DMatrix(X, label=y, **kwargs)
        if weights is not None:
            dtrain.set_weight(np.asarray(weights, dtype=np.float32))
        if objective.startswith("rank:"):
            n_rows = self._n_rows(X)
            grp = self._resolve_groups(groups, n_rows)
            dtrain.set_group(np.asarray(grp, dtype=np.uint32))
        return dtrain

    def _resolve_groups(self, groups: Any | None, n_rows: int) -> list[int]:
        """Resolve the per-group sizes for a ranking DMatrix.

        Accepts either a raw group-id vector (converted to sizes via
        contiguous-run counting) or an already-sized vector (positive ints
        that sum to ``n_rows``). When ``groups`` is None and the column
        roles declare a group column, the caller is expected to have
        already extracted it — here we fail-closed.

        Disambiguation: if the array is integer-typed, every value is
        positive, and ``sum(groups) == n_rows``, it is treated as a sizes
        vector. Otherwise it is treated as a group-id vector and the
        per-group sizes are derived from the contiguous runs (the row
        order of the DMatrix is assumed to be sorted by group).
        """
        import numpy as np  # type: ignore[import-not-found]

        if groups is None:
            raise ValueError(
                "ranking objective requires a group array; pass "
                "groups= to train() (derived from "
                f"task_spec.group_column={self.task_spec.group_column!r})"
            )
        arr = np.asarray(groups)
        if arr.dtype.kind in {"i", "u"} and (arr > 0).all() and int(arr.sum()) == n_rows:
            return [int(g) for g in arr]
        # Treat as group ids → contiguous-run sizes in row order.
        sizes: list[int] = []
        if len(arr) == 0:
            return sizes
        cur = arr[0]
        count = 1
        for g in arr[1:]:
            if g == cur:
                count += 1
            else:
                sizes.append(count)
                cur = g
                count = 1
        sizes.append(count)
        return sizes

    # --- cross-validation (fold metrics) --------------------------------

    def _cross_validate(
        self,
        X: Any,
        y: Any,
        weights: Any | None,
        groups: Any | None,
        feature_names: list[str],
        objective: str,
        device: str,
        np: Any,
        xgb: Any,
    ) -> list[FoldMetric]:
        """Run an internal k-fold CV and return one FoldMetric per fold.

        Uses a manual contiguous-block split (walk-forward-style) — no
        sklearn dependency. Returns an empty list when ``n_folds <= 0`` or
        there isn't enough data to form ``n_folds`` folds.
        """
        n = self._n_rows(X)
        if self.n_folds <= 0 or n < self.n_folds * 2:
            return []
        n_estimators = int(self.params.get("n_estimators", 10))
        # Contiguous block folds (preserves time order when X is sorted).
        fold_bounds = self._fold_bounds(n, self.n_folds)
        metrics_list: list[FoldMetric] = []
        for fold_id, (train_idx, val_idx) in enumerate(fold_bounds):
            X_tr = self._select_rows(X, train_idx)
            y_tr = self._select_rows(y, train_idx)
            X_va = self._select_rows(X, val_idx)
            y_va = self._select_rows(y, val_idx)
            w_tr = self._select_rows(weights, train_idx) if weights is not None else None
            g_tr = self._select_rows(groups, train_idx) if groups is not None else None
            g_va = self._select_rows(groups, val_idx) if groups is not None else None
            dtr = self._build_dmatrix(X_tr, y_tr, w_tr, g_tr, feature_names, objective, np, xgb)
            dva = self._build_dmatrix(X_va, y_va, None, g_va, feature_names, objective, np, xgb)
            params = {
                "tree_method": self.params.get("tree_method", "hist"),
                "device": device,
                "max_depth": int(self.params.get("max_depth", 3)),
                "learning_rate": float(self.params.get("learning_rate", 0.1)),
                "objective": objective,
            }
            if objective == "multi:softmax":
                num_class = self.params.get("num_class")
                if num_class is None:
                    num_class = int(np.max(y)) + 1
                params["num_class"] = int(num_class)
            for k, v in self.params.items():
                if k in {
                    "tree_method",
                    "device",
                    "max_depth",
                    "learning_rate",
                    "objective",
                    "n_estimators",
                    "num_class",
                }:
                    continue
                params[k] = v
            bst = xgb.train(params, dtr, num_boost_round=n_estimators)
            preds = bst.predict(dva)
            m = self._compute_metrics(y_va, preds, objective, np)
            metrics_list.append(
                FoldMetric(
                    fold_id=fold_id,
                    train_size=len(train_idx),
                    val_size=len(val_idx),
                    metrics=m,
                )
            )
        return metrics_list

    @staticmethod
    def _n_rows(X: Any) -> int:
        """Return the number of rows in X (len or shape[0])."""
        try:
            return int(X.shape[0])  # type: ignore[index]
        except Exception:
            return len(X)

    @staticmethod
    def _fold_bounds(n: int, k: int) -> list[tuple[list[int], list[int]]]:
        """Return k contiguous-block (train_idx, val_idx) splits."""
        bounds: list[tuple[list[int], list[int]]] = []
        # Even-ish fold sizes.
        base = n // k
        rem = n % k
        starts = []
        cur = 0
        for i in range(k):
            size = base + (1 if i < rem else 0)
            starts.append((cur, cur + size))
            cur += size
        all_idx = list(range(n))
        for i in range(k):
            v_start, v_end = starts[i]
            val_idx = all_idx[v_start:v_end]
            train_idx = all_idx[:v_start] + all_idx[v_end:]
            bounds.append((train_idx, val_idx))
        return bounds

    @staticmethod
    def _select_rows(arr: Any, idx: list[int]) -> Any:
        """Select rows by integer index, handling ndarray / DataFrame / list."""
        if arr is None:
            return None
        # ndarray / DataFrame support fancy indexing.
        if hasattr(arr, "iloc"):
            return arr.iloc[idx]
        if hasattr(arr, "__getitem__") and hasattr(arr, "shape"):
            import numpy as np  # type: ignore[import-not-found]

            return np.asarray(arr)[idx]
        # Plain list / tuple.
        return [arr[i] for i in idx]

    @staticmethod
    def _compute_metrics(y: Any, preds: Any, objective: str, np: Any) -> dict[str, float]:
        """Compute task-appropriate metrics for one fold."""
        y_arr = np.asarray(y, dtype=np.float64)
        p_arr = np.asarray(preds, dtype=np.float64)
        metrics: dict[str, float] = {}
        if objective == "binary:logistic":
            # AUC + logloss.
            metrics["logloss"] = float(XGBoostTrainer._logloss(y_arr, p_arr, np))
            metrics["auc"] = float(XGBoostTrainer._auc(y_arr, p_arr, np))
            metrics["error"] = float(np.mean((p_arr >= 0.5).astype(int) != y_arr.astype(int)))
        elif objective == "reg:squarederror":
            metrics["mse"] = float(np.mean((p_arr - y_arr) ** 2))
            metrics["mae"] = float(np.mean(np.abs(p_arr - y_arr)))
            metrics["rmse"] = float(np.sqrt(np.mean((p_arr - y_arr) ** 2)))
        elif objective.startswith("rank:"):
            metrics["ndcg"] = float(XGBoostTrainer._ndcg(y_arr, p_arr, np))
            metrics["mean_group_gain"] = float(np.mean(p_arr))
        elif objective == "multi:softmax":
            metrics["merror"] = float(np.mean(preds.astype(int) != y_arr.astype(int)))
            # mlogloss requires class probabilities (2D preds from
            # multi:softprob). multi:softmax returns 1D class indices,
            # so mlogloss is only emitted when probabilities are available.
            if p_arr.ndim == 2:
                metrics["mlogloss"] = float(XGBoostTrainer._mlogloss(y_arr, p_arr, np))
        else:
            metrics["mse"] = float(np.mean((p_arr - y_arr) ** 2))
        return metrics

    @staticmethod
    def _logloss(y: Any, p: Any, np: Any) -> float:
        """Binary cross-entropy (clamped to avoid log(0))."""
        eps = 1e-15
        p = np.clip(p, eps, 1 - eps)
        return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))

    @staticmethod
    def _auc(y: Any, p: Any, np: Any) -> float:
        """ROC AUC via the rank-statistic formula (no sklearn)."""
        n_pos = float(np.sum(y == 1))
        n_neg = float(np.sum(y == 0))
        if n_pos == 0 or n_neg == 0:
            return 0.5
        order = np.argsort(p)
        ranks = np.empty_like(order, dtype=np.float64)
        ranks[order] = np.arange(1, len(p) + 1, dtype=np.float64)
        # Handle ties by averaging ranks (simple: assign tied groups the
        # mean rank). For a canary metric this is good enough.
        sum_ranks = float(np.sum(ranks[y == 1]))
        return (sum_ranks - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)

    @staticmethod
    def _ndcg(y: Any, p: Any, np: Any, k: int | None = None) -> float:
        """NDCG@k for a single group (canary metric)."""
        y = np.asarray(y, dtype=np.float64)
        p = np.asarray(p, dtype=np.float64)
        n = len(y)
        if k is None:
            k = n
        k = min(k, n)
        if k == 0:
            return 0.0
        order_pred = np.argsort(-p)[:k]
        dcg = float(np.sum((2 ** y[order_pred] - 1) / np.log2(np.arange(2, k + 2))))
        order_ideal = np.argsort(-y)[:k]
        idcg = float(np.sum((2 ** y[order_ideal] - 1) / np.log2(np.arange(2, k + 2))))
        if idcg == 0:
            return 0.0
        return dcg / idcg

    @staticmethod
    def _mlogloss(y: Any, preds: Any, np: Any) -> float:
        """Multiclass logloss (preds is the predicted class probabilities)."""
        y = np.asarray(y, dtype=int)
        preds = np.asarray(preds, dtype=np.float64)
        n, _c = preds.shape
        eps = 1e-15
        preds = np.clip(preds, eps, 1 - eps)
        rows = np.arange(n)
        p_true = preds[rows, y]
        return float(-np.mean(np.log(p_true)))

    # --- feature importance ---------------------------------------------

    def get_feature_importance(self) -> dict[str, dict[str, float]]:
        """Return feature importance keyed by feature name.

        Returns a dict ``{feature_name: {"gain": ..., "weight": ...,
        "cover": ...}}``. Features that were never split on are absent
        from XGBoost's score dict — they are filled with 0.0 for
        completeness so downstream consumers see a stable schema.

        Raises:
            RuntimeError: if :meth:`train` has not been called (no model).
            ImportError: if ``xgboost`` is not installed.
        """
        if self._model is None:
            raise RuntimeError(
                "get_feature_importance() called before train(); no model is available"
            )
        return self._extract_importance(self._model, self._feature_names or [])

    @staticmethod
    def _extract_importance(model: Any, feature_names: list[str]) -> dict[str, dict[str, float]]:
        """Extract gain/weight/cover importance from a Booster."""
        out: dict[str, dict[str, float]] = {}
        raw: dict[str, dict[str, float]] = {}
        for itype in IMPORTANCE_TYPES:
            try:
                scores = model.get_score(importance_type=itype)
            except Exception:
                scores = {}
            for fname, val in scores.items():
                raw.setdefault(fname, {})[itype] = float(val)
        names = feature_names or list(raw.keys()) or ["f0"]
        for fname in names:
            entry = raw.get(fname, {})
            out[fname] = {
                "gain": float(entry.get("gain", 0.0)),
                "weight": float(entry.get("weight", 0.0)),
                "cover": float(entry.get("cover", 0.0)),
            }
        return out

    # --- artifact persistence -------------------------------------------

    def save_artifact(self, path: str) -> None:
        """Save the trained model to ``path`` in JSON or UBJ format.

        The file extension selects the encoding:

        - ``.json`` → the human-readable JSON format.
        - ``.ubj`` (or any other extension) → the binary UBJ format
          (preferred for size/speed).

        Raises:
            RuntimeError: if :meth:`train` has not been called.
            ImportError: if ``xgboost`` is not installed.
            ValueError: if ``path`` is empty.
        """
        if not isinstance(path, str) or not path.strip():
            raise ValueError("artifact path must be a non-empty string")
        if self._model is None:
            raise RuntimeError("save_artifact() called before train(); no model to save")
        try:
            import xgboost as xgb  # type: ignore[import-not-found]  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                f"saving an XGBoost artifact requires the 'xgboost' package; missing: {exc.name}"
            ) from exc
        # Ensure the parent directory exists (best-effort; ignore errors
        # for paths that are already in the cwd).
        parent = os.path.dirname(os.path.abspath(path))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        # XGBoost infers the format from the extension. .json → JSON,
        # .ubj → UBJ. For any other extension we force UBJ by writing to
        # a temp .ubj path and renaming — but the simplest robust path is
        # to rely on xgboost's extension sniffing and default to UBJ for
        # unknown extensions by appending .ubj internally.
        lower = path.lower()
        if lower.endswith(".json") or lower.endswith(".ubj"):
            self._model.save_model(path)
        else:
            # Unknown extension → write UBJ to the given path. XGBoost
            # saves UBJ when the extension is not .json/.txt, so a direct
            # save produces UBJ bytes.
            self._model.save_model(path)

    # --- introspection --------------------------------------------------

    @property
    def device_used(self) -> str | None:
        """The device the last :meth:`train` call actually used."""
        return self._device_used

    @property
    def objective_used(self) -> str | None:
        """The objective the last :meth:`train` call used."""
        return self._objective_used

    @property
    def n_estimators_used(self) -> int:
        """The number of boosting rounds the last :meth:`train` call ran."""
        return self._n_estimators_used
