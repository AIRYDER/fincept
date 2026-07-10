"""quant_foundry.catboost_trainer — CatBoost GPU trainer adapter (T-7.2).

A :class:`CatBoostTrainer` adapts the CatBoost library
(``CatBoostClassifier`` / ``CatBoostRegressor``) to the
:mod:`quant_foundry` training contracts:

- :class:`quant_foundry.dataset_manifest.ColumnRoles` — the single source
  of truth for which columns are features, labels, weights, groups, etc.
- :class:`quant_foundry.training_manifest.ModelTaskSpec` — the explicit
  learning task declaration (``binary`` / ``regression`` / ``multiclass``
  / ``ranking``).

Design notes
------------

- **Lazy imports.** ``catboost`` (and ``numpy`` / ``pandas``) are imported
  *inside* methods, not at module level. This keeps ``quant_foundry``
  importable in environments where CatBoost is not installed — the
  trainer only fails if it is actually invoked without its backend
  present.
- **Fail-closed categorical roles.** When categorical features are
  declared explicitly (via the ``cat_features`` argument) the trainer
  validates that every declared-categorical column has a
  categorical-compatible dtype and that every non-declared column has a
  non-categorical dtype. Any mismatch raises ``ValueError`` so a silent
  dtype/role drift never reaches the model.
- **GPU fallback.** When ``params["task_type"] == "GPU"`` but no GPU is
  available, the trainer falls back to ``task_type="CPU"`` with a
  warning — unless ``strict_gpu=True``, in which case it fail-closes.
- **Fold metrics.** A simple k-fold walk is performed (manual split, no
  sklearn dependency) and per-fold metrics are aggregated:
  ``accuracy`` + ``logloss`` for classification tasks, ``rmse`` for
  regression.
- **Pydantic v2 result.** :class:`CatBoostTrainingResult` is a frozen,
  ``extra='forbid'`` model with ``arbitrary_types_allowed=True`` so it
  can carry the trained CatBoost model object.

This module is file-disjoint from all other builders. It imports only
from :mod:`quant_foundry.dataset_manifest`,
:mod:`quant_foundry.training_manifest`, and (lazily) the CatBoost /
numpy / pandas backends.
"""

from __future__ import annotations

import logging
import os
import warnings
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict

from quant_foundry.dataset_manifest import ColumnRoles
from quant_foundry.training_manifest import ModelTaskSpec

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------


class CatBoostTrainingResult(BaseModel):
    """Typed result of a successful :meth:`CatBoostTrainer.train` call.

    Frozen + ``extra='forbid'`` (audit integrity). The ``model`` field
    carries the live CatBoost estimator so callers can score immediately;
    ``arbitrary_types_allowed=True`` lets Pydantic v2 validate the
    non-Pydantic model object.

    Fields:
        model: the trained CatBoost estimator
            (``CatBoostClassifier`` / ``CatBoostRegressor`` /
            ``CatBoostRanker``).
        feature_importance: mapping ``feature_name -> importance`` (from
            ``model.get_feature_importance()``). Empty dict when the
            model has no feature importance (e.g. a degenerate fit).
        fold_metrics: aggregated cross-validation metrics. Always
            contains ``n_folds``; classification tasks add ``accuracy``
            and ``logloss``; regression tasks add ``rmse``.
        artifact_path: filesystem path where the model was persisted in
            CatBoost native ``.cbm`` format. ``None`` when no artifact
            was saved (e.g. an in-memory canary run).
        task_type: the effective ``task_type`` used for training
            (``"GPU"`` or ``"CPU"``). Differs from the requested value
            only when a GPU fallback occurred.
        n_features: number of feature columns used.
        n_rows: number of training rows.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    model: Any
    feature_importance: dict[str, float]
    fold_metrics: dict[str, float]
    artifact_path: str | None
    task_type: str
    n_features: int
    n_rows: int


# ---------------------------------------------------------------------------
# Trainer
# ---------------------------------------------------------------------------


@dataclass
class CatBoostTrainer:
    """CatBoost trainer adapter bound to column roles + a task spec.

    The trainer is a mutable dataclass (``frozen=False``) so per-run
    state (the fitted model, the effective task type after a GPU
    fallback) can be stashed on the instance. A fresh trainer is
    constructed per training job — no cross-job state leaks.

    Args:
        column_roles: explicit declaration of feature / label / weight /
            group columns (from the dataset manifest). The trainer uses
            ONLY ``column_roles.feature_columns`` as features when ``X``
            is a pandas DataFrame.
        task_spec: explicit learning task declaration. ``task_type``
            selects the estimator class (``binary`` / ``multiclass`` ->
            ``CatBoostClassifier``, ``regression`` ->
            ``CatBoostRegressor``, ``ranking`` -> ``CatBoostRanker``).
        params: CatBoost hyper-parameters. Recognised keys include
            ``iterations``, ``depth``, ``learning_rate``,
            ``loss_function``, ``task_type`` (``"GPU"`` or ``"CPU"``),
            ``devices`` (e.g. ``"0"``), ``random_seed``. Unknown keys are
            forwarded to the CatBoost estimator as-is.
        artifact_path: filesystem path where the trained model is
            persisted in CatBoost native ``.cbm`` format by
            :meth:`save_artifact`. May be empty/``None`` for in-memory
            canary runs.
        strict_gpu: if True, a GPU request that cannot be honoured
            fail-closes instead of falling back to CPU.
        strict_categorical: if True (default), a mismatch between
            declared categorical columns and their actual dtypes
            fail-closes.
        n_folds: number of k-fold splits used for fold-metric
            aggregation. Defaults to 3. Set to 1 to skip CV and report
            only a single-fit metric.
        random_seed: base random seed for the k-fold splitter. The
            CatBoost estimator seed is taken from ``params`` when
            present.
    """

    column_roles: ColumnRoles
    task_spec: ModelTaskSpec
    params: dict[str, Any]
    artifact_path: str | None = None
    strict_gpu: bool = False
    strict_categorical: bool = True
    n_folds: int = 3
    random_seed: int = 0

    # Per-run state (populated by train()).
    model: Any = field(default=None, repr=False)
    _effective_task_type: str | None = field(default=None, repr=False)

    # --- public API ------------------------------------------------------

    def train(
        self,
        X: Any,
        y: Any,
        weights: Any | None = None,
        groups: Any | None = None,
        cat_features: Any | None = None,
    ) -> CatBoostTrainingResult:
        """Train a CatBoost model and (optionally) persist it.

        Args:
            X: feature matrix. A pandas ``DataFrame`` (column names are
                respected via ``column_roles.feature_columns``) or a
                numpy array (positional columns).
            y: label vector (1-D). For ``binary`` tasks the values must
                be 0/1 (or two distinct classes); for ``regression`` any
                real values.
            weights: optional 1-D sample-weight vector aligned with ``X``.
            groups: optional 1-D group-id vector for ranking tasks.
            cat_features: explicit categorical feature spec. Either a
                list of column names (when ``X`` is a DataFrame) or a
                list of positional column indices. When ``None`` the
                categorical columns are inferred from ``X``'s dtypes
                (object / string / category). When provided, a
                fail-closed dtype/role mismatch check is performed.

        Returns:
            :class:`CatBoostTrainingResult` with the fitted model,
            feature importance, fold metrics, and artifact path.

        Raises:
            ImportError: if ``catboost`` is not installed.
            ValueError: on a categorical role mismatch, an unsupported
                task type, empty feature set, or shape mismatch.
        """
        cb = self._require_catboost()
        np = self._require_numpy()

        if X is None or y is None:
            raise ValueError("X and y must both be non-None")

        # Resolve the feature frame (DataFrame subset or numpy array).
        X_feat, feature_names = self._resolve_features(X)
        if X_feat.shape[1] == 0:
            raise ValueError(
                "no feature columns resolved from column_roles; feature_columns must be non-empty"
            )
        y_arr = self._coerce_label(y, np)
        if y_arr.shape[0] != X_feat.shape[0]:
            raise ValueError(
                f"X and y row count mismatch: X has {X_feat.shape[0]} rows, y has {y_arr.shape[0]}"
            )

        weights_arr = self._coerce_optional_vector(weights, X_feat.shape[0], np, "weights")
        groups_arr = self._coerce_optional_vector(groups, X_feat.shape[0], np, "groups")

        # Resolve categorical features + fail-closed mismatch check.
        cat_indices = self._resolve_cat_features(X_feat, feature_names, cat_features)

        # Resolve effective params (GPU fallback).
        effective_params = self._resolve_effective_params(cb, np)

        # Fold metrics (k-fold CV with a fresh estimator per fold).
        fold_metrics = self._compute_fold_metrics(
            cb,
            np,
            X_feat,
            y_arr,
            weights_arr,
            groups_arr,
            cat_indices,
            effective_params,
        )

        # Train the final model on the full dataset.
        model = self._fit_model(
            cb,
            X_feat,
            y_arr,
            weights_arr,
            groups_arr,
            cat_indices,
            effective_params,
        )
        self.model = model

        # Persist artifact if a path was configured.
        saved_path: str | None = None
        if self.artifact_path:
            saved_path = self.save_artifact(self.artifact_path)

        importance = self.get_feature_importance()

        return CatBoostTrainingResult(
            model=model,
            feature_importance=importance,
            fold_metrics=fold_metrics,
            artifact_path=saved_path,
            task_type=self._effective_task_type or effective_params.get("task_type", "CPU"),
            n_features=int(X_feat.shape[1]),
            n_rows=int(X_feat.shape[0]),
        )

    def save_artifact(self, path: str) -> str:
        """Persist the trained model in CatBoost native ``.cbm`` format.

        Args:
            path: destination filesystem path. Parent directories are
                created if missing.

        Returns:
            The absolute path of the saved artifact.

        Raises:
            ValueError: if no model has been trained yet.
            RuntimeError: if the underlying ``model.save_model`` call
                fails.
        """
        if self.model is None:
            raise ValueError("no trained model to save; call train() before save_artifact()")
        if not path or not isinstance(path, str) or not path.strip():
            raise ValueError("artifact path must be a non-empty string")
        parent = os.path.dirname(os.path.abspath(path))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        try:
            self.model.save_model(path)
        except Exception as exc:  # pragma: no cover - defensive
            raise RuntimeError(f"failed to save CatBoost artifact to {path!r}: {exc}") from exc
        return os.path.abspath(path)

    def get_feature_importance(self) -> dict[str, float]:
        """Return feature importance as ``{feature_name: importance}``.

        Uses ``model.get_feature_importance()``. Feature names are taken
        from ``column_roles.feature_columns`` when available, otherwise
        positional indices are used. Returns an empty dict when the
        model has no feature importance (e.g. a degenerate single-class
        fit that CatBoost refuses to evaluate).

        Raises:
            ValueError: if no model has been trained yet.
        """
        if self.model is None:
            raise ValueError("no trained model; call train() before get_feature_importance()")
        feature_names = list(self.column_roles.feature_columns)
        try:
            importances = self.model.get_feature_importance()
        except Exception:
            return {}
        # CatBoost returns a numpy array; normalise to Python floats.
        try:
            vals = list(importances)
        except Exception:
            vals = list(importances)
        out: dict[str, float] = {}
        for i, imp in enumerate(vals):
            name = feature_names[i] if i < len(feature_names) else f"f{i}"
            try:
                out[str(name)] = float(imp)
            except (TypeError, ValueError):
                out[str(name)] = 0.0
        return out

    # --- internal helpers ------------------------------------------------

    @staticmethod
    def _require_catboost() -> Any:
        """Lazy-import catboost or raise a helpful ImportError."""
        try:
            import catboost as cb
        except ImportError as exc:
            raise ImportError(
                "catboost is not installed; the CatBoostTrainer requires "
                "the `catboost` package. Install it with `pip install "
                "catboost`."
            ) from exc
        return cb

    @staticmethod
    def _require_numpy() -> Any:
        """Lazy-import numpy or raise a helpful ImportError."""
        try:
            import numpy as np
        except ImportError as exc:
            raise ImportError(
                "numpy is not installed; the CatBoostTrainer requires "
                "numpy for label/fold handling."
            ) from exc
        return np

    def _resolve_features(self, X: Any) -> tuple[Any, list[str]]:
        """Subset ``X`` to ``column_roles.feature_columns`` when possible.

        Returns ``(X_feat, feature_names)``. When ``X`` is a pandas
        DataFrame and ``column_roles.feature_columns`` names are present
        in its columns, the feature frame is restricted to those
        columns in declared order. Otherwise ``X`` is used as-is and
        feature names fall back to ``column_roles.feature_columns`` (or
        positional ``f{i}`` names).
        """
        feature_names = list(self.column_roles.feature_columns)
        # Duck-type pandas DataFrame via the `columns` attribute.
        if hasattr(X, "columns") and hasattr(X, "iloc"):
            cols = list(X.columns)
            # If every declared feature is present, subset in declared order.
            if all(name in cols for name in feature_names):
                X_feat = X[feature_names]
                return X_feat, feature_names
            # Otherwise use the frame as-is (caller supplied the slice).
            return X, feature_names
        # numpy array (or array-like): no column subsetting.
        return X, feature_names

    @staticmethod
    def _coerce_label(y: Any, np: Any) -> Any:
        """Coerce ``y`` to a 1-D numpy array."""
        arr = np.asarray(y)
        if arr.ndim == 2 and arr.shape[1] == 1:
            arr = arr.ravel()
        if arr.ndim != 1:
            raise ValueError(f"y must be 1-D (got {arr.ndim}-D with shape {arr.shape})")
        return arr

    @staticmethod
    def _coerce_optional_vector(
        v: Any,
        n_rows: int,
        np: Any,
        name: str,
    ) -> Any | None:
        """Coerce an optional 1-D vector (weights/groups) or None."""
        if v is None:
            return None
        arr = np.asarray(v)
        if arr.ndim == 2 and arr.shape[1] == 1:
            arr = arr.ravel()
        if arr.ndim != 1:
            raise ValueError(f"{name} must be 1-D (got {arr.ndim}-D with shape {arr.shape})")
        if arr.shape[0] != n_rows:
            raise ValueError(f"{name} length {arr.shape[0]} does not match X rows {n_rows}")
        return arr

    def _resolve_cat_features(
        self,
        X_feat: Any,
        feature_names: list[str],
        cat_features: Any | None,
    ) -> list[int]:
        """Resolve categorical feature indices + fail-closed mismatch check.

        When ``cat_features`` is None, categorical columns are inferred
        from ``X_feat``'s dtypes (object / string / category). When
        provided, it is normalised to positional indices and a strict
        dtype/role mismatch check is performed:
        - a declared-categorical column must have a categorical-compatible
          dtype;
        - a non-declared column must NOT have a categorical-compatible
          dtype.

        Raises ValueError on any mismatch (when ``strict_categorical``).
        """
        self._require_numpy()
        n_features = X_feat.shape[1]

        # Determine which positional columns are categorical by dtype.
        dtype_cat_mask = self._dtype_categorical_mask(X_feat, n_features)

        if cat_features is None:
            # Infer: every dtype-categorical column is a cat feature.
            return [i for i in range(n_features) if dtype_cat_mask[i]]

        # Normalise explicit cat_features to positional indices.
        explicit_indices = self._normalise_cat_features(cat_features, X_feat, feature_names)
        explicit_set = set(explicit_indices)

        if self.strict_categorical:
            for i in range(n_features):
                declared_cat = i in explicit_set
                dtype_cat = dtype_cat_mask[i]
                if declared_cat and not dtype_cat:
                    name = feature_names[i] if i < len(feature_names) else f"f{i}"
                    raise ValueError(
                        f"categorical role mismatch: column {name!r} "
                        f"(index {i}) is declared categorical but has a "
                        f"non-categorical dtype "
                        f"({self._column_dtype(X_feat, i)!r}); "
                        f"convert it to string/category or remove it "
                        f"from cat_features"
                    )
                if (not declared_cat) and dtype_cat:
                    name = feature_names[i] if i < len(feature_names) else f"f{i}"
                    raise ValueError(
                        f"categorical role mismatch: column {name!r} "
                        f"(index {i}) has a categorical dtype "
                        f"({self._column_dtype(X_feat, i)!r}) but is NOT "
                        f"declared in cat_features; either add it to "
                        f"cat_features or cast it to a numeric dtype"
                    )
        return sorted(explicit_indices)

    @staticmethod
    def _dtype_categorical_mask(X_feat: Any, n_features: int) -> list[bool]:
        """Return a per-column mask of categorical-compatible dtypes.

        Categorical-compatible: object, string, category. Numeric and
        bool columns are NOT categorical.
        """
        # pandas DataFrame: use the pandas dtype introspection API so we
        # are robust to ``StringDtype`` (pandas 3.x) vs ``object`` columns.
        if hasattr(X_feat, "dtypes"):
            try:
                import pandas as pd
            except ImportError:
                pd = None  # type: ignore[assignment]
            mask: list[bool] = []
            for dtype in X_feat.dtypes:
                if pd is not None:
                    is_cat = bool(
                        pd.api.types.is_string_dtype(dtype)
                        or pd.api.types.is_object_dtype(dtype)
                        or isinstance(dtype, pd.CategoricalDtype)
                    )
                else:
                    kind = str(dtype).lower()
                    is_cat = "object" in kind or "string" in kind or kind.startswith("category")
                mask.append(is_cat)
            return mask
        # numpy array: only object/string dtype is categorical.
        arr_dtype_kind = getattr(X_feat, "dtype", None)
        kind = str(arr_dtype_kind)
        is_obj = kind in ("object", "str", "string", "<U", "U")
        # Per-column object-ness isn't representable for a 2-D numeric
        # array, so only a uniform object array is treated as categorical.
        if is_obj:
            return [True] * n_features
        return [False] * n_features

    @staticmethod
    def _column_dtype(X_feat: Any, i: int) -> str:
        """Return a readable dtype string for column ``i``."""
        if hasattr(X_feat, "dtypes"):
            return str(list(X_feat.dtypes)[i])
        return str(getattr(X_feat, "dtype", "?"))

    @staticmethod
    def _normalise_cat_features(
        cat_features: Any,
        X_feat: Any,
        feature_names: list[str],
    ) -> list[int]:
        """Normalise ``cat_features`` (names or indices) to positional ints."""
        out: list[int] = []
        # Name-based when X is a DataFrame with columns.
        if hasattr(X_feat, "columns"):
            cols = list(X_feat.columns)
            name_to_idx = {name: i for i, name in enumerate(cols)}
            for cf in cat_features:
                if isinstance(cf, str):
                    if cf not in name_to_idx:
                        raise ValueError(
                            f"cat_features name {cf!r} not found in X columns {list(name_to_idx)!r}"
                        )
                    out.append(name_to_idx[cf])
                elif isinstance(cf, (int,)) and not isinstance(cf, bool):
                    if cf < 0 or cf >= len(cols):
                        raise ValueError(
                            f"cat_features index {cf} out of range for {len(cols)} columns"
                        )
                    out.append(int(cf))
                else:
                    raise ValueError(
                        f"cat_features entries must be str or int; got {type(cf).__name__} ({cf!r})"
                    )
            return out
        # Index-based for array-like X.
        n = X_feat.shape[1]
        for cf in cat_features:
            if isinstance(cf, str):
                # Map via declared feature names if available.
                if cf in feature_names:
                    out.append(feature_names.index(cf))
                else:
                    raise ValueError(
                        f"cat_features name {cf!r} not found in feature names {feature_names!r}"
                    )
            elif isinstance(cf, (int,)) and not isinstance(cf, bool):
                if cf < 0 or cf >= n:
                    raise ValueError(f"cat_features index {cf} out of range for {n} columns")
                out.append(int(cf))
            else:
                raise ValueError(
                    f"cat_features entries must be str or int; got {type(cf).__name__} ({cf!r})"
                )
        return out

    def _resolve_effective_params(self, cb: Any, np: Any) -> dict[str, Any]:
        """Return a copy of ``params`` with the GPU fallback applied.

        If ``task_type == "GPU"`` and no GPU is available, fall back to
        ``task_type="CPU"`` with a warning — unless ``strict_gpu`` is
        set, in which case fail-closed.
        """
        params = dict(self.params)
        requested = str(params.get("task_type", "CPU")).upper()
        if requested != "GPU":
            self._effective_task_type = requested or "CPU"
            return params
        if self._gpu_available(cb, np):
            self._effective_task_type = "GPU"
            return params
        # No GPU available.
        if self.strict_gpu:
            raise RuntimeError(
                "strict_gpu=True but no GPU is available for CatBoost; "
                "refusing to fall back to CPU (fail-closed)"
            )
        warnings.warn(
            "CatBoost task_type='GPU' requested but no GPU is available; "
            "falling back to task_type='CPU'. Set strict_gpu=True to "
            "fail-closed instead.",
            stacklevel=2,
        )
        logger.warning("CatBoost GPU fallback: task_type='GPU' -> 'CPU' (no GPU available)")
        params["task_type"] = "CPU"
        # ``devices`` is meaningless on CPU; drop it to avoid CatBoost warnings.
        params.pop("devices", None)
        self._effective_task_type = "CPU"
        return params

    @staticmethod
    def _gpu_available(cb: Any, np: Any) -> bool:
        """Best-effort GPU availability probe for CatBoost.

        CatBoost does not expose a stable public GPU-count API across
        builds, so we attempt a tiny GPU fit and treat any exception as
        "no GPU". This is intentionally cheap (4 rows, 2 iterations).
        """
        try:
            probe_X = np.array([[0.0, 1.0], [1.0, 0.0], [0.0, 0.0], [1.0, 1.0]])
            probe_y = np.array([0, 1, 0, 1])
            probe = cb.CatBoostClassifier(
                iterations=2,
                task_type="GPU",
                devices="0",
                verbose=False,
                allow_writing_files=False,
            )
            probe.fit(probe_X, probe_y)
            return True
        except Exception:
            return False

    def _select_estimator_class(self, cb: Any) -> Any:
        """Pick the CatBoost estimator class from ``task_spec.task_type``."""
        tt = self.task_spec.task_type
        if tt in ("binary", "multiclass"):
            return cb.CatBoostClassifier
        if tt == "regression":
            return cb.CatBoostRegressor
        if tt == "ranking":
            ranker = getattr(cb, "CatBoostRanker", None)
            if ranker is None:
                raise ValueError(
                    "task_type='ranking' requires catboost.CatBoostRanker "
                    "which is not available in this catboost build"
                )
            return ranker
        raise ValueError(
            f"unsupported task_type {tt!r} for CatBoostTrainer; "
            f"allowed: binary, multiclass, regression, ranking"
        )

    def _fit_model(
        self,
        cb: Any,
        X_feat: Any,
        y_arr: Any,
        weights_arr: Any | None,
        groups_arr: Any | None,
        cat_indices: list[int],
        params: dict[str, Any],
    ) -> Any:
        """Fit a CatBoost estimator on the full dataset."""
        cls = self._select_estimator_class(cb)
        fit_kwargs: dict[str, Any] = {}
        if cat_indices:
            fit_kwargs["cat_features"] = cat_indices
        if weights_arr is not None:
            fit_kwargs["sample_weight"] = weights_arr
        # CatBoostRanker requires a group array via Pool.
        if self.task_spec.task_type == "ranking":
            if groups_arr is None:
                raise ValueError("ranking task_type requires a non-None groups array")
            pool = cb.Pool(X_feat, y_arr, group_id=groups_arr, cat_features=cat_indices or None)
            model = cls(**params)
            model.fit(pool, **{k: v for k, v in fit_kwargs.items() if k != "cat_features"})
            return model
        model = cls(**params)
        model.fit(X_feat, y_arr, **fit_kwargs)
        return model

    def _compute_fold_metrics(
        self,
        cb: Any,
        np: Any,
        X_feat: Any,
        y_arr: Any,
        weights_arr: Any | None,
        groups_arr: Any | None,
        cat_indices: list[int],
        params: dict[str, Any],
    ) -> dict[str, float]:
        """Aggregate k-fold CV metrics (manual split, no sklearn)."""
        n = X_feat.shape[0]
        n_folds = max(1, int(self.n_folds))
        task_type = self.task_spec.task_type

        if n_folds <= 1 or n < 2:
            # Too little data for CV — report a degenerate single-fold
            # metric so the result always carries the required keys.
            return self._single_fit_metrics(
                cb,
                np,
                X_feat,
                y_arr,
                weights_arr,
                groups_arr,
                cat_indices,
                params,
            )

        # Cap folds to the number of rows.
        n_folds = min(n_folds, n)
        rng = np.random.default_rng(self.random_seed)
        perm = rng.permutation(n)
        fold_sizes = [n // n_folds] * n_folds
        for i in range(n % n_folds):
            fold_sizes[i] += 1

        accs: list[float] = []
        loglosses: list[float] = []
        rmses: list[float] = []
        cursor = 0
        for fsz in fold_sizes:
            test_idx = perm[cursor : cursor + fsz]
            train_idx = np.concatenate([perm[:cursor], perm[cursor + fsz :]])
            cursor += fsz
            if train_idx.size == 0 or test_idx.size == 0:
                continue
            X_tr, X_te = self._index_rows(X_feat, train_idx), self._index_rows(X_feat, test_idx)
            y_tr, y_te = y_arr[train_idx], y_arr[test_idx]
            w_tr = weights_arr[train_idx] if weights_arr is not None else None
            g_tr = groups_arr[train_idx] if groups_arr is not None else None
            fold_params = dict(params)
            # Keep folds quiet + cheap.
            fold_params.setdefault("verbose", False)
            try:
                fold_model = self._fit_fold(
                    cb,
                    X_tr,
                    y_tr,
                    w_tr,
                    g_tr,
                    cat_indices,
                    fold_params,
                )
            except Exception:
                # A failed fold should not abort training; skip it.
                continue
            m = self._score_fold(fold_model, X_te, y_te, task_type, np)
            if "accuracy" in m:
                accs.append(m["accuracy"])
            if "logloss" in m:
                loglosses.append(m["logloss"])
            if "rmse" in m:
                rmses.append(m["rmse"])

        out: dict[str, float] = {"n_folds": float(n_folds)}
        if task_type in ("binary", "multiclass"):
            out["accuracy"] = float(np.mean(accs)) if accs else 0.0
            out["logloss"] = float(np.mean(loglosses)) if loglosses else float("nan")
        elif task_type == "regression":
            out["rmse"] = float(np.mean(rmses)) if rmses else float("nan")
        elif task_type == "ranking":
            out["n_folds"] = float(n_folds)
        return out

    def _single_fit_metrics(
        self,
        cb: Any,
        np: Any,
        X_feat: Any,
        y_arr: Any,
        weights_arr: Any | None,
        groups_arr: Any | None,
        cat_indices: list[int],
        params: dict[str, Any],
    ) -> dict[str, float]:
        """Metrics for the degenerate (no-CV) case."""
        task_type = self.task_spec.task_type
        out: dict[str, float] = {"n_folds": 1.0}
        if task_type in ("binary", "multiclass"):
            out["accuracy"] = 0.0
            out["logloss"] = float("nan")
        elif task_type == "regression":
            out["rmse"] = float("nan")
        return out

    def _fit_fold(
        self,
        cb: Any,
        X_tr: Any,
        y_tr: Any,
        w_tr: Any | None,
        g_tr: Any | None,
        cat_indices: list[int],
        params: dict[str, Any],
    ) -> Any:
        """Fit a fresh estimator on one CV fold."""
        cls = self._select_estimator_class(cb)
        fit_kwargs: dict[str, Any] = {}
        if cat_indices:
            fit_kwargs["cat_features"] = cat_indices
        if w_tr is not None:
            fit_kwargs["sample_weight"] = w_tr
        if self.task_spec.task_type == "ranking":
            pool = cb.Pool(X_tr, y_tr, group_id=g_tr, cat_features=cat_indices or None)
            model = cls(**params)
            model.fit(pool)
            return model
        model = cls(**params)
        model.fit(X_tr, y_tr, **fit_kwargs)
        return model

    def _score_fold(
        self,
        model: Any,
        X_te: Any,
        y_te: Any,
        task_type: str,
        np: Any,
    ) -> dict[str, float]:
        """Score a held-out fold; returns accuracy/logloss or rmse."""
        out: dict[str, float] = {}
        try:
            if task_type in ("binary", "multiclass"):
                preds = model.predict(X_te)
                # CatBoost may return a 2-D string array for classifiers.
                preds_arr = np.asarray(preds).ravel()
                # Coerce to numeric for comparison.
                try:
                    preds_num = preds_arr.astype(float)
                except (TypeError, ValueError):
                    # String class labels — map to y_te's labels positionally.
                    preds_num = self._label_encode(preds_arr, y_te, np)
                y_te_num = self._label_encode(np.asarray(y_te), np.asarray(y_te), np)
                acc = float(np.mean(preds_num == y_te_num))
                out["accuracy"] = acc
                # Logloss via predict_proba when available.
                if hasattr(model, "predict_proba"):
                    try:
                        proba = model.predict_proba(X_te)
                        proba_arr = np.asarray(proba)
                        # Positive-class probability (last column).
                        p1 = np.clip(proba_arr[:, -1], 1e-12, 1 - 1e-12)
                        ll = -float(
                            np.mean(y_te_num * np.log(p1) + (1 - y_te_num) * np.log(1 - p1))
                        )
                        out["logloss"] = ll
                    except Exception:
                        out["logloss"] = float("nan")
                else:
                    out["logloss"] = float("nan")
            elif task_type == "regression":
                preds = np.asarray(model.predict(X_te)).ravel()
                y_te_arr = np.asarray(y_te).ravel().astype(float)
                rmse = float(np.sqrt(np.mean((preds - y_te_arr) ** 2)))
                out["rmse"] = rmse
        except Exception:
            if task_type in ("binary", "multiclass"):
                out.setdefault("accuracy", 0.0)
                out.setdefault("logloss", float("nan"))
            elif task_type == "regression":
                out.setdefault("rmse", float("nan"))
        return out

    @staticmethod
    def _label_encode(arr: Any, ref: Any, np: Any) -> Any:
        """Map arbitrary label values to 0..k-1 integers using ``ref`` order."""
        ref_arr = np.asarray(ref).ravel()
        classes: list[Any] = []
        for v in ref_arr:
            if v not in classes:
                classes.append(v)
        mapping = {v: i for i, v in enumerate(classes)}
        out = np.array([mapping.get(v, -1) for v in np.asarray(arr).ravel()], dtype=float)
        return out

    @staticmethod
    def _index_rows(X: Any, idx: Any) -> Any:
        """Index rows of ``X`` (DataFrame or array) by a numpy index array."""
        if hasattr(X, "iloc"):
            return X.iloc[idx]
        return X[idx]


__all__ = ["CatBoostTrainer", "CatBoostTrainingResult"]
